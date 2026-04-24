from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.agents.general import GeneralPurposeAgent, Workspace
from alphasolve.config.agent_config import AlphaSolveConfig

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
    theorem_check_file: Path | None = None
    solved_problem: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)


class GeneratorDigestContext:
    def __init__(self, *, worker_id: int, worker_rel: str) -> None:
        self.worker_id = worker_id
        self.worker_rel = worker_rel
        self._reasoning_since_last_subagent: list[dict[str, Any]] = []

    def record_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "thinking":
            return
        content = str(event.get("content") or "")
        if not content.strip():
            return
        self._reasoning_since_last_subagent.append(
            {
                "turn": event.get("turn"),
                "content": content,
            }
        )

    def consume(self, subagent_call: dict[str, Any]) -> dict[str, Any]:
        reasoning = self._reasoning_since_last_subagent
        self._reasoning_since_last_subagent = []
        return {
            "caller_role": "generator",
            "worker_id": self.worker_id,
            "worker_dir": self.worker_rel,
            "subagent_type": subagent_call.get("agent_type"),
            "subagent_session_id": subagent_call.get("session_id"),
            "subagent_task": subagent_call.get("task"),
            "reasoning_since_previous_subagent": reasoning,
        }


class LemmaWorker:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        worker_id: int,
        worker_hint: str | None = None,
        max_verify_rounds: int = 2,
        verifier_scaling_factor: int = 1,
        subagent_max_depth: int = 2,
        renderer: LemmaTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        digest_queue: KnowledgeDigestQueue | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.worker_id = worker_id
        self.worker_hint = worker_hint
        self.max_verify_rounds = max(1, int(max_verify_rounds))
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = max(0, int(subagent_max_depth))
        self.workspace = Workspace(layout.workspace_dir)
        self.worker_dir = layout.unverified_dir / f"lemma-{worker_id:04d}-{uuid.uuid4().hex[:8]}"
        self.worker_rel = self.worker_dir.relative_to(layout.workspace_dir).as_posix()
        self.trace: list[dict[str, Any]] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.digest_queue = digest_queue
        self.stop_event = stop_event

    def run(self) -> LemmaWorkerRunResult:
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        self._set_phase("starting", status="running")
        if self._should_stop():
            return self._finish("cancelled", "lemmaworker cancelled because another worker solved the problem")
        if self.worker_hint:
            (self.worker_dir / "worker_hint.md").write_text(self.worker_hint, encoding="utf-8")

        try:
            lemma_file = self._run_generator()
            if self._should_stop():
                return self._finish(
                    "cancelled",
                    "lemmaworker cancelled because another worker solved the problem",
                    lemma_file=lemma_file,
                )
            if lemma_file is None:
                return self._finish("rejected", "generator did not produce a lemma markdown file")

            review_file: Path | None = None
            for round_index in range(1, self.max_verify_rounds + 1):
                if self._should_stop():
                    return self._finish(
                        "cancelled",
                        "lemmaworker cancelled because another worker solved the problem",
                        lemma_file=lemma_file,
                        review_file=review_file,
                    )
                review_text = self._run_verifier(lemma_file, round_index=round_index)
                review_file = self.worker_dir / "review.md"
                review_file.write_text(review_text, encoding="utf-8")
                if _is_pass_verdict(review_text):
                    if self._should_stop():
                        return self._finish(
                            "cancelled",
                            "lemmaworker cancelled because another worker solved the problem",
                            lemma_file=lemma_file,
                            review_file=review_file,
                        )
                    verified = self._copy_to_verified(lemma_file)
                    solved_problem, theorem_check_text = self._run_theorem_checks(verified)
                    theorem_check_file = None
                    if solved_problem:
                        theorem_check_file = self.worker_dir / "theorem_check.md"
                        theorem_check_file.write_text(theorem_check_text, encoding="utf-8")
                    return self._finish(
                        "verified",
                        "成功产生一个引理，lemma statement 如下："
                        + _extract_statement(lemma_file.read_text(encoding="utf-8")),
                        lemma_file=lemma_file,
                        verified_file=verified,
                        review_file=review_file,
                        theorem_check_file=theorem_check_file,
                        solved_problem=solved_problem,
                    )
                if round_index < self.max_verify_rounds:
                    self._run_reviser(lemma_file, review_text, round_index=round_index)
                    if self._should_stop():
                        return self._finish(
                            "cancelled",
                            "lemmaworker cancelled because another worker solved the problem",
                            lemma_file=lemma_file,
                            review_file=review_file,
                        )

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
        digest_context = GeneratorDigestContext(worker_id=self.worker_id, worker_rel=self.worker_rel)
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/generator",
            digest_queue=self.digest_queue,
            digest_context_provider=digest_context.consume,
        )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents),
            event_sink=self._generator_event_sink(digest_context),
        )
        result = agent.run(self._generator_task())
        self.trace.append({"role": "generator", "trace": result.trace, "final_answer": result.final_answer})
        return self._find_lemma_file()

    def _run_verifier(self, lemma_file: Path, *, round_index: int) -> str:
        attempt_reviews: list[dict[str, Any]] = []
        config_names = self._verifier_config_names()
        for attempt_index in range(1, self.verifier_scaling_factor + 1):
            if self._should_stop():
                break
            config_name = config_names[(attempt_index - 1) % len(config_names)]
            review_text = self._run_verifier_attempt(
                lemma_file,
                round_index=round_index,
                attempt_index=attempt_index,
                config_name=config_name,
            )
            attempt_reviews.append(
                {
                    "attempt": attempt_index,
                    "config": config_name,
                    "passed": _is_pass_verdict(review_text),
                    "review": review_text,
                }
            )
            if not _is_pass_verdict(review_text):
                break
        return _combine_verifier_reviews(attempt_reviews, self.verifier_scaling_factor)

    def _run_verifier_attempt(self, lemma_file: Path, *, round_index: int, attempt_index: int, config_name: str) -> str:
        role = f"verifier r{round_index}.{attempt_index}"
        self._set_phase(role, status="thinking")
        verifier_workspace = self.worker_dir / "verifier_workspace" / f"round-{round_index:02d}" / f"attempt-{attempt_index:02d}"
        verifier_workspace.mkdir(parents=True, exist_ok=True)
        verifier_rel = verifier_workspace.relative_to(self.layout.workspace_dir).as_posix()
        all_verifier_ws_rel = (self.worker_dir / "verifier_workspace").relative_to(self.layout.workspace_dir).as_posix()
        config = self.suite.agents[config_name]
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            write_root_rel=verifier_rel,
            deny_read_rel=all_verifier_ws_rel,
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/verifier-r{round_index}-a{attempt_index}-{config_name}",
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=self.workspace,
                worker_rel=self.worker_rel,
                deny_other_unverified=True,
                write_root_rel=verifier_rel,
                deny_read_rel=all_verifier_ws_rel,
            ),
            digest_queue=self.digest_queue,
        )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents),
            event_sink=self._event_sink(role),
        )
        result = agent.run(
            self._verifier_task(
                lemma_file,
                round_index=round_index,
                attempt_index=attempt_index,
                attempt_total=self.verifier_scaling_factor,
                config_name=config_name,
            )
        )
        self.trace.append({
            "role": "verifier",
            "round": round_index,
            "attempt": attempt_index,
            "config": config_name,
            "trace": result.trace,
            "final_answer": result.final_answer,
        })
        if self.digest_queue is not None:
            from .knowledge_digest import DigestTask
            self.digest_queue.submit(DigestTask(
                trace_segment=[{"role": "verifier", "content": result.final_answer}],
                source_label=f"{self.worker_dir.name}/verifier-r{round_index}-a{attempt_index}-{config_name}",
            ))
        return result.final_answer

    def _run_theorem_checks(self, verified_file: Path) -> tuple[bool, str]:
        attempts: list[str] = []
        for attempt_index in range(1, AlphaSolveConfig.CHECK_IS_THEOREM_TIMES + 1):
            if self._should_stop():
                return False, _format_theorem_check_attempts(attempts)
            check_text = self._run_theorem_checker(verified_file, attempt_index=attempt_index)
            attempts.append(check_text)
            if not _solves_problem(check_text):
                return False, _format_theorem_check_attempts(attempts)
        return True, _format_theorem_check_attempts(attempts)

    def _run_theorem_checker(self, verified_file: Path, *, attempt_index: int) -> str:
        role = "theorem_checker"
        self._set_phase(role, status="thinking")
        config = self.suite.agents["theorem_checker"]
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/theorem-checker",
            digest_queue=self.digest_queue,
        )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(access, allow_write=False, subagent_service=subagents),
            event_sink=self._event_sink(role),
        )
        result = agent.run(self._theorem_checker_task(verified_file, attempt_index=attempt_index))
        self.trace.append({
            "role": "theorem_checker",
            "attempt": attempt_index,
            "trace": result.trace,
            "final_answer": result.final_answer,
        })
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
            if path.name not in {"review.md", "theorem_check.md", "worker_hint.md"} and path.is_file()
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
                    "\\ref{filename-without-extension}."
                ),
            ]
            if part
        )

    def _verifier_task(
        self,
        lemma_file: Path,
        *,
        round_index: int,
        attempt_index: int,
        attempt_total: int,
        config_name: str,
    ) -> str:
        rel = lemma_file.relative_to(self.layout.workspace_dir).as_posix()
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Candidate Lemma File\n"
            + rel
            + "\n\nRead the candidate lemma and write a rigorous review. Your final answer must include `Verdict: pass` "
            "or `Verdict: fail`. Check that every `\\ref{...}` points to an existing verified lemma "
            "filename without the `.md` extension. Do not judge whether this lemma solves the original problem; "
            "that is handled by a separate theorem checker."
            + f"\n\nVerification round: {round_index}"
            + f"\nIndependent verification attempt: {attempt_index} of {attempt_total}"
            + f"\nVerifier config: {config_name}"
        )

    def _theorem_checker_task(self, verified_file: Path, *, attempt_index: int) -> str:
        rel = verified_file.relative_to(self.layout.workspace_dir).as_posix()
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Newly Verified Lemma File\n"
            + rel
            + "\n\nDecide whether the newly verified lemma, together with any verified lemmas cited by "
            "`\\ref{filename-without-extension}`, proves the original problem. Read cited verified lemmas as needed. "
            "Do not re-review the lemma proof except to understand what has been established. Your final answer must "
            "include exactly one line `Solves original problem: yes` or `Solves original problem: no`."
            + f"\n\nIndependent theorem check attempt: {attempt_index} of {AlphaSolveConfig.CHECK_IS_THEOREM_TIMES}"
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
        theorem_check_file: Path | None = None,
        solved_problem: bool = False,
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
            theorem_check_file=theorem_check_file,
            solved_problem=solved_problem,
            trace=list(self.trace),
        )

    def _event_sink(self, role: str):
        return make_worker_event_sink(self.renderer, worker_id=self.worker_id, role=role)

    def _generator_event_sink(self, digest_context: GeneratorDigestContext):
        worker_sink = self._event_sink("generator")

        def sink(event: dict[str, Any]) -> None:
            digest_context.record_event(event)
            if worker_sink is not None:
                worker_sink(event)

        return sink

    def _set_phase(self, phase: str, *, status: str) -> None:
        if self.renderer is not None:
            self.renderer.update_phase(self.worker_id, phase, status=status)

    def _should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _verifier_config_names(self) -> list[str]:
        raw = self.suite.settings.get("verifier_agents") or ["verifier"]
        if isinstance(raw, str):
            names = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            names = [str(item).strip() for item in raw if str(item).strip()]
        if not names:
            names = ["verifier"]
        missing = [name for name in names if name not in self.suite.agents]
        if missing:
            raise ValueError(f"unknown verifier agent config(s): {missing}")
        return names


def _is_pass_verdict(text: str) -> bool:
    for line in (text or "").splitlines():
        clean = line.strip().lower()
        if not clean:
            continue
        if clean.startswith("verdict:"):
            return clean.split(":", 1)[1].strip().startswith("pass")
    lowered = (text or "").lower()
    return "\\boxed{valid}" in lowered or "boxed{valid}" in lowered


def _combine_verifier_reviews(attempt_reviews: list[dict[str, Any]], expected_attempts: int) -> str:
    failed = [item for item in attempt_reviews if not item["passed"]]
    verdict = "fail" if failed or len(attempt_reviews) < expected_attempts else "pass"
    parts = [
        f"Verdict: {verdict}",
        "",
        "# Independent Verifier Attempts",
        "",
    ]
    if len(attempt_reviews) < expected_attempts:
        parts.extend([
            f"Only {len(attempt_reviews)} of {expected_attempts} verifier attempts completed.",
            "",
        ])
    for item in attempt_reviews:
        status = "pass" if item["passed"] else "fail"
        parts.extend([
            f"## Attempt {item['attempt']} ({item['config']}): {status}",
            "",
            str(item["review"]).strip(),
            "",
        ])
    return "\n".join(parts).rstrip() + "\n"


def _solves_problem(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"solves\s+original\s+problem\s*:\s*yes\b", lowered))


def _format_theorem_check_attempts(attempts: list[str]) -> str:
    parts = ["# Theorem Check", ""]
    for index, text in enumerate(attempts, start=1):
        parts.extend([f"## Attempt {index}", "", text.strip(), ""])
    return "\n".join(parts).rstrip() + "\n"


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
