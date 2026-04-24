from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.agents.general import GeneralPurposeAgent, Workspace

from .dashboard import make_worker_event_sink
from .project import ProjectLayout
from .tools import ClientFactory, RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway
    from alphasolve.utils.rich_renderer import LemmaTeamRenderer
    from .knowledge_digest import KnowledgeDigestQueue


@dataclass(frozen=True)
class LemmaWorkerRunResult:
    worker_id: int
    worker_dir: Path
    status: str
    summary: str
    lemma_file: Path | None = None
    verified_file: Path | None = None
    review_file: Path | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


class FilesystemLemmaWorker:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        worker_id: int,
        worker_hint: str | None = None,
        max_verify_rounds: int = 2,
        subagent_max_depth: int = 2,
        renderer: LemmaTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        digest_queue: KnowledgeDigestQueue | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.worker_id = worker_id
        self.worker_hint = worker_hint
        self.max_verify_rounds = max(1, int(max_verify_rounds))
        self.subagent_max_depth = max(0, int(subagent_max_depth))
        self.workspace = Workspace(layout.workspace_dir)
        self.worker_dir = layout.unverified_dir / f"lemma-{worker_id:04d}-{uuid.uuid4().hex[:8]}"
        self.worker_rel = self.worker_dir.relative_to(layout.workspace_dir).as_posix()
        self.trace: list[dict[str, Any]] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.digest_queue = digest_queue

    def run(self) -> LemmaWorkerRunResult:
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        self._set_phase("starting", status="running")
        if self.worker_hint:
            (self.worker_dir / "worker_hint.md").write_text(self.worker_hint, encoding="utf-8")

        try:
            lemma_file = self._run_generator()
            if lemma_file is None:
                return self._finish("rejected", "generator did not produce a lemma markdown file")

            review_file: Path | None = None
            for round_index in range(1, self.max_verify_rounds + 1):
                review_text = self._run_verifier(lemma_file, round_index=round_index)
                review_file = self.worker_dir / "review.md"
                review_file.write_text(review_text, encoding="utf-8")
                if _is_pass_verdict(review_text):
                    verified = self._copy_to_verified(lemma_file)
                    return self._finish(
                        "verified",
                        "成功产生一个引理，lemma statement 如下："
                        + _extract_statement(lemma_file.read_text(encoding="utf-8")),
                        lemma_file=lemma_file,
                        verified_file=verified,
                        review_file=review_file,
                    )
                if round_index < self.max_verify_rounds:
                    self._run_reviser(lemma_file, review_text, round_index=round_index)

            summary = "未能产生通过验证的引理。"
            if lemma_file.exists():
                summary += "\n\n" + lemma_file.read_text(encoding="utf-8")[:4000]
            if review_file is not None and review_file.exists():
                summary += "\n\n最终审稿意见：\n" + review_file.read_text(encoding="utf-8")[:4000]
            return self._finish("rejected", summary, lemma_file=lemma_file, review_file=review_file)
        except Exception as exc:
            return self._finish("failed", str(exc))

    def _run_generator(self) -> Path | None:
        self._set_phase("generator", status="thinking")
        config = self.suite.agents["generator"]
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            single_lemma_file=True,
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/generator",
            digest_queue=self.digest_queue,
        )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents),
            event_sink=self._event_sink("generator"),
        )
        result = agent.run(self._generator_task())
        self.trace.append({"role": "generator", "trace": result.trace, "final_answer": result.final_answer})
        return self._find_lemma_file()

    def _run_verifier(self, lemma_file: Path, *, round_index: int) -> str:
        role = f"verifier r{round_index}"
        self._set_phase(role, status="thinking")
        verifier_workspace = self.worker_dir / "verifier_workspace"
        verifier_workspace.mkdir(parents=True, exist_ok=True)
        config = self.suite.agents["verifier"]
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            write_root_rel=(verifier_workspace.relative_to(self.layout.workspace_dir).as_posix()),
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/verifier-r{round_index}",
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=self.workspace,
                worker_rel=self.worker_rel,
                deny_other_unverified=True,
                read_root_rel=(verifier_workspace.relative_to(self.layout.workspace_dir).as_posix()),
                write_root_rel=(verifier_workspace.relative_to(self.layout.workspace_dir).as_posix()),
            ),
            digest_queue=self.digest_queue,
        )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents),
            event_sink=self._event_sink(role),
        )
        result = agent.run(self._verifier_task(lemma_file, round_index=round_index))
        self.trace.append({"role": "verifier", "round": round_index, "trace": result.trace, "final_answer": result.final_answer})
        return result.final_answer

    def _run_reviser(self, lemma_file: Path, review_text: str, *, round_index: int) -> None:
        role = f"reviser r{round_index}"
        self._set_phase(role, status="thinking")
        config = self.suite.agents["reviser"]
        exact_rel = lemma_file.relative_to(self.layout.workspace_dir).as_posix()
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            exact_write_rel=exact_rel,
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/reviser-r{round_index}",
            digest_queue=self.digest_queue,
        )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents),
            event_sink=self._event_sink(role),
        )
        result = agent.run(self._reviser_task(lemma_file, review_text, round_index=round_index))
        self.trace.append({"role": "reviser", "round": round_index, "trace": result.trace, "final_answer": result.final_answer})

    def _find_lemma_file(self) -> Path | None:
        candidates = [
            path
            for path in self.worker_dir.glob("*.md")
            if path.name not in {"review.md", "worker_hint.md"} and path.is_file()
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda path: path.stat().st_mtime)[-1]

    def _copy_to_verified(self, lemma_file: Path) -> Path:
        target = self.layout.verified_dir / lemma_file.name
        if target.exists():
            target = self.layout.verified_dir / f"{lemma_file.stem}-{uuid.uuid4().hex[:6]}{lemma_file.suffix}"
        shutil.copy2(lemma_file, target)
        return target

    def _generator_task(self) -> str:
        return "\n\n".join(
            part
            for part in [
                "# Problem",
                self.layout.read_problem(),
                "# User Hint",
                self.layout.read_hint(),
                "# Worker Hint",
                self.worker_hint,
                "# Output",
                (
                    "Create exactly one lemma markdown file directly in your own directory "
                    f"`{self.worker_rel}`. The filename should be a concise abstract of the lemma, "
                    "for example `compactness-criterion.md`, not a numbered lemma name. The file must contain "
                    "a Statement section and a Proof section. You may reference verified lemmas with "
                    "\\ref{verified lemma abstract}."
                ),
            ]
            if part
        )

    def _verifier_task(self, lemma_file: Path, *, round_index: int) -> str:
        rel = lemma_file.relative_to(self.layout.workspace_dir).as_posix()
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Candidate Lemma File\n"
            + rel
            + "\n\nRead the candidate lemma and write a rigorous review. Your final answer must include `Verdict: pass` "
            "or `Verdict: fail`."
            + f"\n\nVerification round: {round_index}"
        )

    def _reviser_task(self, lemma_file: Path, review_text: str, *, round_index: int) -> str:
        rel = lemma_file.relative_to(self.layout.workspace_dir).as_posix()
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Candidate Lemma File\n"
            + rel
            + "\n\n# Review\n"
            + review_text
            + "\n\nRewrite the same lemma markdown file in place, addressing every review issue."
            + f"\n\nRevision round: {round_index}"
        )

    def _finish(
        self,
        status: str,
        summary: str,
        *,
        lemma_file: Path | None = None,
        verified_file: Path | None = None,
        review_file: Path | None = None,
    ) -> LemmaWorkerRunResult:
        self._set_phase("done", status=status)
        trace_path = self.worker_dir / "trace.json"
        trace_path.write_text(json.dumps(self.trace, ensure_ascii=False, indent=2), encoding="utf-8")
        return LemmaWorkerRunResult(
            worker_id=self.worker_id,
            worker_dir=self.worker_dir,
            status=status,
            summary=summary,
            lemma_file=lemma_file,
            verified_file=verified_file,
            review_file=review_file,
            trace=list(self.trace),
        )

    def _event_sink(self, role: str):
        return make_worker_event_sink(self.renderer, worker_id=self.worker_id, role=role)

    def _set_phase(self, phase: str, *, status: str) -> None:
        if self.renderer is not None:
            self.renderer.update_phase(self.worker_id, phase, status=status)


def _is_pass_verdict(text: str) -> bool:
    lowered = (text or "").lower()
    return "verdict: pass" in lowered or "\\boxed{valid}" in lowered or "boxed{valid}" in lowered


def _extract_statement(text: str) -> str:
    lowered = text.lower()
    marker = "## statement"
    start = lowered.find(marker)
    if start == -1:
        return "\n" + text[:800]
    start = text.find("\n", start)
    if start == -1:
        return "\n" + text[:800]
    next_heading = text.find("\n## ", start + 1)
    if next_heading == -1:
        return "\n" + text[start:].strip()[:800]
    return "\n" + text[start:next_heading].strip()[:800]
