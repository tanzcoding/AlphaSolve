from __future__ import annotations

import json
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agents.general import GeneralAgentConfig, GeneralPurposeAgent, Workspace
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.utils.event_logger import compose_event_sinks
from .dashboard import make_worker_event_sink
from .project import ProjectLayout
from .tools import ClientFactory, RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry, register_agent_tool

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway
    from alphasolve.utils.log_session import LogSession
    from alphasolve.utils.rich_renderer import PropositionTeamRenderer
    from .curator import CuratorQueue


_REVIEW_VERDICT_PROMPT = """You are an AlphaSolve verifier-attempt verdict classifier.

Your job is to read one isolated verifier-attempt review and classify whether the candidate proposition passed that attempt.

Rules:
- Interpret nested verifier verdicts semantically; Markdown decoration such as `**Verdict: pass**` must not change its meaning.
- Decide from the mathematical substance of this one review.
- Return exactly one lowercase word: `pass` or `fail`.
- Do not include Markdown, punctuation, explanation, or any other text.
- Return `pass` only if the review establishes that the proposition is correct, complete, and rigorous.
- Do not judge whether the proposition solves the original problem; a separate theorem checker handles that.
"""


_REMOVE_RETRY_DELAYS = (0.1, 0.3, 0.7)


def _make_path_writable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    except OSError:
        return


def _make_tree_writable(path: Path) -> None:
    _make_path_writable(path)
    if not path.is_dir() or path.is_symlink():
        return
    try:
        children = list(path.rglob("*"))
    except OSError:
        return
    for child in children:
        _make_path_writable(child)


def _rmtree_onerror(func, path, _exc_info) -> None:
    # Windows/OneDrive 有时会把目录标成只读，先放宽属性再重试删除。
    _make_path_writable(Path(path))
    func(path)


def _remove_path_once(path: Path) -> None:
    _make_tree_writable(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onerror=_rmtree_onerror)
    else:
        path.unlink()


def _remove_path_with_retries(path: Path) -> None:
    last_error: OSError | None = None
    for delay in (0.0, *_REMOVE_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            _remove_path_once(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            # 这类错误常见于 Windows 文件句柄短暂未释放或 OneDrive 正在同步。
            if getattr(exc, "winerror", None) not in {5, 32, 33, None}:
                raise
    if last_error is not None:
        raise last_error


def _empty_directory_with_retries(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in list(path.iterdir()):
        _remove_path_with_retries(child)


def _reset_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    try:
        _remove_path_with_retries(path)
    except OSError:
        if not path.is_dir():
            raise
        # 如果 Windows/OneDrive 拒绝删除目录本身，就退而求其次清空其内容。
        _make_tree_writable(path)
        _empty_directory_with_retries(path)
        return
    path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class WorkerRunResult:
    worker_id: str
    worker_dir: Path
    status: str
    summary: str
    proposition_file: Path | None = None
    verified_file: Path | None = None
    review_file: Path | None = None
    theorem_check_file: Path | None = None
    solved_problem: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class VerifierWorkflowResult:
    review_text: str
    passed: bool
    review_file: Path | None
    attempts_run: int


class GeneratorCuratorContext:
    def __init__(self, *, worker_id: str, worker_rel: str) -> None:
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


class Worker:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        worker_hint: str | None = None,
        max_verify_rounds: int = 2,
        verifier_scaling_factor: int = 1,
        subagent_max_depth: int = 2,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        stop_event: threading.Event | None = None,
        log_session: LogSession | None = None,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        prop_hash = uuid.uuid4().hex[:8]
        self.worker_id = prop_hash
        self.worker_hint = worker_hint
        self.max_verify_rounds = max(1, int(max_verify_rounds))
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = max(0, int(subagent_max_depth))
        self.workspace = Workspace(layout.workspace_dir)
        self.worker_dir = layout.unverified_dir / f"prop-{prop_hash}"
        self.worker_rel = self.worker_dir.relative_to(layout.workspace_dir).as_posix()
        self.trace: list[dict[str, Any]] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.stop_event = stop_event
        self.log_session = log_session
        self.progress_callback = progress_callback
        self._worker_log_sink = log_session.create_worker_sink(prop_hash) if log_session is not None else None

    def run(self) -> WorkerRunResult:
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        self._set_phase("starting", status="running")
        if self._should_stop():
            return self._finish("cancelled", "worker cancelled because another worker solved the problem")
        if self.worker_hint:
            (self.worker_dir / "worker_hint.md").write_text(self.worker_hint, encoding="utf-8")

        try:
            try:
                proposition_file = self._run_generator()
                if self._should_stop():
                    return self._finish(
                        "cancelled",
                        "worker cancelled because another worker solved the problem",
                        proposition_file=proposition_file,
                    )
                if proposition_file is None:
                    return self._finish("rejected", "generator did not produce a proposition markdown file")

                final_review_file: Path | None = None
                last_review_text = ""
                for workflow_index in range(1, self.max_verify_rounds + 1):
                    if self._should_stop():
                        return self._finish(
                            "cancelled",
                            "worker cancelled because another worker solved the problem",
                            proposition_file=proposition_file,
                        )
                    workflow_result = self._run_verifier_workflow(proposition_file, workflow_index=workflow_index)
                    last_review_text = workflow_result.review_text
                    final_review_file = workflow_result.review_file
                    if self._should_stop():
                        return self._finish(
                            "cancelled",
                            "worker cancelled because another worker solved the problem",
                            proposition_file=proposition_file,
                        )
                    if workflow_result.passed:
                        if self._should_stop():
                            return self._finish(
                                "cancelled",
                                "worker cancelled because another worker solved the problem",
                                proposition_file=proposition_file,
                            )
                        verified = self._copy_to_verified(proposition_file)
                        solved_problem, theorem_check_text = self._run_theorem_checks(verified)
                        theorem_check_file = None
                        if solved_problem:
                            theorem_check_file = self.worker_dir / "theorem_check.md"
                            theorem_check_file.write_text(theorem_check_text, encoding="utf-8")
                        return self._finish(
                            "verified",
                            "Successfully produced a verified proposition. Statement: "
                            + _extract_statement(proposition_file.read_text(encoding="utf-8")),
                            proposition_file=proposition_file,
                            verified_file=verified,
                            review_file=final_review_file,
                            theorem_check_file=theorem_check_file,
                            solved_problem=solved_problem,
                        )
                    if workflow_index < self.max_verify_rounds:
                        self._run_reviser(proposition_file, workflow_result.review_text, workflow_index=workflow_index)
                        if self._should_stop():
                            return self._finish(
                                "cancelled",
                                "worker cancelled because another worker solved the problem",
                                proposition_file=proposition_file,
                            )

                summary = "Failed to produce a verified proposition."
                if proposition_file.exists():
                    summary += "\n\n" + proposition_file.read_text(encoding="utf-8")[:4000]
                if last_review_text and (final_review_file is None or not final_review_file.exists()):
                    final_review_file = self._write_final_review(last_review_text)
                if final_review_file is not None and final_review_file.exists():
                    summary += "\n\nFinal review:\n" + final_review_file.read_text(encoding="utf-8")[:4000]
                return self._finish("rejected", summary, proposition_file=proposition_file, review_file=final_review_file)
            except Exception as exc:
                return self._finish("failed", str(exc))
        finally:
            if self._worker_log_sink is not None:
                self._worker_log_sink.close()

    def _run_generator(self) -> Path | None:
        config = self.suite.agents["generator"]
        self._set_phase("generator", status="thinking", model=self._model_name(config))
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            single_proposition_file=True,
        )
        curator_context = GeneratorCuratorContext(worker_id=self.worker_id, worker_rel=self.worker_rel)
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/generator",
            curator_queue=self.curator_queue,
            curator_context_provider=curator_context.consume,
            log_session=self.log_session,
            stop_event=self.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=self.workspace,
                worker_rel=self.worker_rel,
                deny_other_unverified=True,
            ),
        )
        registry = build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents)
        register_agent_tool(registry, agent_config=config, subagent_service=subagents)
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            event_sink=self._generator_event_sink(curator_context),
            stop_event=self.stop_event,
        )
        result = agent.run(self._generator_task())
        self.trace.append({"role": "generator", "trace": result.trace, "final_answer": result.final_answer})
        return self._find_proposition_file()

    def _run_verifier_workflow(self, proposition_file: Path, *, workflow_index: int) -> VerifierWorkflowResult:
        self._reset_verifier_workflow_workspace(proposition_file)
        config_names = self._verifier_config_names()
        last_review_text = ""
        last_review_file: Path | None = None
        for attempt_index in range(1, self.verifier_scaling_factor + 1):
            if self._should_stop():
                break
            self._clear_attempt_review()
            config_name = config_names[(attempt_index - 1) % len(config_names)]
            last_review_text = self._run_verifier_attempt_agent(
                proposition_file,
                workflow_index=workflow_index,
                attempt_index=attempt_index,
                config_name=config_name,
            )
            last_review_file = self._write_final_review(last_review_text)
            verdict = self._run_review_verdict_judge(
                last_review_text,
                workflow_index=workflow_index,
                attempt_index=attempt_index,
            )
            if verdict != "pass":
                self.trace.append({
                    "role": "verifier_workflow",
                    "workflow": workflow_index,
                    "attempts_run": attempt_index,
                    "verdict": "fail",
                })
                return VerifierWorkflowResult(last_review_text, False, last_review_file, attempt_index)
        attempts_run = self.verifier_scaling_factor if last_review_text else 0
        passed = attempts_run == self.verifier_scaling_factor and bool(last_review_text)
        self.trace.append({
            "role": "verifier_workflow",
            "workflow": workflow_index,
            "attempts_run": attempts_run,
            "verdict": "pass" if passed else "fail",
        })
        return VerifierWorkflowResult(last_review_text, passed, last_review_file, attempts_run)

    def _run_verifier_attempt_agent(self, proposition_file: Path, *, workflow_index: int, attempt_index: int, config_name: str) -> str:
        role = f"verifier_attempt w{workflow_index}.{attempt_index}"
        config = self._verifier_attempt_config(config_name)
        self._set_phase(role, status="thinking", model=self._model_name(config))
        all_verifier_ws_rel = (self.worker_dir / "verifier_workspace").relative_to(self.layout.workspace_dir).as_posix()
        deny_read_rels = (all_verifier_ws_rel,)
        if config_name == "verifier_citation":
            deny_read_rels = ("knowledge", *deny_read_rels)
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            deny_read_rels=deny_read_rels,
            deny_read_file_names=("review.md",),
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/verifier-workflow-{workflow_index}-attempt-{attempt_index}-{config_name}",
            log_session=self.log_session,
            stop_event=self.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=self.workspace,
                worker_rel=self.worker_rel,
                deny_other_unverified=True,
                deny_read_rels=deny_read_rels,
                deny_read_file_names=("review.md",),
            ),
            curator_queue=self.curator_queue,
        )
        registry = build_workspace_tool_registry(access, allow_write=False, subagent_service=subagents)
        register_agent_tool(registry, agent_config=config, subagent_service=subagents)
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            event_sink=self._event_sink(role),
            stop_event=self.stop_event,
        )
        result = agent.run(
            self._verifier_task(
                proposition_file,
                workflow_index=workflow_index,
                attempt_index=attempt_index,
                attempt_total=self.verifier_scaling_factor,
                config_name=config_name,
            )
        )
        self.trace.append({
            "role": "verifier_attempt",
            "workflow": workflow_index,
            "attempt": attempt_index,
            "config": config_name,
            "trace": result.trace,
            "final_answer": result.final_answer,
        })
        if self.curator_queue is not None:
            from .curator import CuratorTask
            self.curator_queue.submit(CuratorTask(
                trace_segment=[{"role": "verifier_attempt", "content": result.final_answer}],
                source_label=f"{self.worker_dir.name}/verifier-workflow-{workflow_index}-attempt-{attempt_index}-{config_name}",
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
        config = self.suite.agents["theorem_checker"]
        self._set_phase(role, status="thinking", model=self._model_name(config))
        access = RoleWorkspaceAccess(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            deny_other_unverified=True,
            read_root_rel="verified_propositions",
        )
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            session_prefix=f"{self.worker_dir.name}/theorem-checker",
            curator_queue=self.curator_queue,
            log_session=self.log_session,
            stop_event=self.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=self.workspace,
                worker_rel=self.worker_rel,
                deny_other_unverified=True,
                read_root_rel="verified_propositions",
            ),
        )
        registry = build_workspace_tool_registry(access, allow_write=False, subagent_service=subagents)
        register_agent_tool(registry, agent_config=config, subagent_service=subagents)
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            event_sink=self._event_sink(role),
            stop_event=self.stop_event,
        )
        result = agent.run(self._theorem_checker_task(verified_file, attempt_index=attempt_index))
        self.trace.append({
            "role": "theorem_checker",
            "attempt": attempt_index,
            "trace": result.trace,
            "final_answer": result.final_answer,
        })
        return result.final_answer

    def _run_reviser(self, proposition_file: Path, review_text: str, *, workflow_index: int) -> None:
        role = f"reviser w{workflow_index}"
        config = self.suite.agents["reviser"]
        self._set_phase(role, status="thinking", model=self._model_name(config))
        exact_rel = proposition_file.relative_to(self.layout.workspace_dir).as_posix()
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
            session_prefix=f"{self.worker_dir.name}/reviser-workflow-{workflow_index}",
            curator_queue=self.curator_queue,
            log_session=self.log_session,
            stop_event=self.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=self.workspace,
                worker_rel=self.worker_rel,
                deny_other_unverified=True,
            ),
        )
        registry = build_workspace_tool_registry(access, allow_write=True, subagent_service=subagents)
        register_agent_tool(registry, agent_config=config, subagent_service=subagents)
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            event_sink=self._event_sink(role),
            stop_event=self.stop_event,
        )
        result = agent.run(self._reviser_task(proposition_file, review_text, workflow_index=workflow_index))
        self.trace.append({"role": "reviser", "workflow": workflow_index, "trace": result.trace, "final_answer": result.final_answer})

    def _run_review_verdict_judge(self, review_text: str, *, workflow_index: int, attempt_index: int) -> str:
        role = f"review_verdict_judge w{workflow_index}.{attempt_index}"
        base_config = self.suite.agents.get("verifier") or self.suite.agents[self._verifier_config_names()[0]]
        config = GeneralAgentConfig(
            name="review_verdict_judge",
            system_prompt=_REVIEW_VERDICT_PROMPT,
            tools=[],
            max_turns=base_config.max_turns,
            model_config=base_config.model_config,
        )
        self._set_phase(role, status="thinking", model=self._model_name(config))
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_workspace_tool_registry(
                RoleWorkspaceAccess(
                    workspace=self.workspace,
                    worker_rel=self.worker_rel,
                    deny_other_unverified=True,
                ),
                allow_write=False,
            ),
            event_sink=self._event_sink(role),
            stop_event=self.stop_event,
        )
        result = agent.run(self._review_verdict_task(review_text, workflow_index=workflow_index, attempt_index=attempt_index))
        verdict = _parse_review_verdict(result.final_answer)
        self.trace.append({
            "role": "review_verdict_judge",
            "workflow": workflow_index,
            "attempt": attempt_index,
            "verdict": verdict,
            "trace": result.trace,
            "final_answer": result.final_answer,
        })
        return verdict

    def _find_proposition_file(self) -> Path | None:
        candidates = [
            path
            for path in self.worker_dir.glob("*.md")
            if path.name not in {"review.md", "theorem_check.md", "worker_hint.md"} and path.is_file()
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda path: path.stat().st_mtime)[-1]

    def _copy_to_verified(self, proposition_file: Path) -> Path:
        name = self._generate_proposition_name(proposition_file)
        target = self.layout.verified_dir / name
        if target.exists():
            target = self.layout.verified_dir / f"{target.stem}-{uuid.uuid4().hex[:6]}.md"
        shutil.copy2(proposition_file, target)
        return target

    def _generate_proposition_name(self, proposition_file: Path) -> str:
        content = proposition_file.read_text(encoding="utf-8")[:3000]
        config = self.suite.agents.get("generator") or next(iter(self.suite.agents.values()))
        prompt = (
            "Read the following verified mathematical proposition and return a short kebab-case filename "
            "(2-5 words, lowercase, hyphens only, no extension) that captures its mathematical content. "
            "Examples: energy-identity-bootstrap, compactness-criterion, sobolev-embedding-estimate. "
            "Return ONLY the filename, nothing else.\n\n"
            + content
        )
        try:
            client = self.client_factory(config)
            response = client.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            )
            raw = (response.get("content") or "").strip().lower()
            name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
            name = re.sub(r"-{2,}", "-", name)
            if name and len(name) <= 80:
                return name + ".md"
        except Exception:
            pass
        return f"proposition-{uuid.uuid4().hex[:8]}.md"

    def _write_final_review(self, review_text: str) -> Path:
        path = self.worker_dir / "review.md"
        path.write_text(review_text, encoding="utf-8")
        return path

    def _clear_attempt_review(self) -> None:
        review_file = self.worker_dir / "review.md"
        if review_file.exists():
            _remove_path_with_retries(review_file)

    def _reset_verifier_workflow_workspace(self, proposition_file: Path) -> None:
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        protected = {
            proposition_file.resolve(),
            (self.worker_dir / "worker_hint.md").resolve(),
        }
        verifier_workspace = self.worker_dir / "verifier_workspace"
        for child in list(self.worker_dir.iterdir()):
            if child.resolve() in protected:
                continue
            if child == verifier_workspace:
                _reset_directory(verifier_workspace)
            else:
                _remove_path_with_retries(child)
        verifier_workspace.mkdir(parents=True, exist_ok=True)

    def _clear_verifier_artifacts(self) -> None:
        self._clear_attempt_review()
        verifier_workspace = self.worker_dir / "verifier_workspace"
        _reset_directory(verifier_workspace)

    def _generator_task(self) -> str:
        return "\n\n".join(
            part
            for part in [
                "# Problem",
                self.layout.read_problem(),
                "# General Hint",
                self.layout.read_hint(),
                "# Task Guidance",
                self.worker_hint,
                "# Output",
                (
                    "Create a file named `proposition.md` directly in your own directory "
                    f"`{self.worker_rel}`. The file must contain "
                    "a Statement section and a Proof section. You may reference verified propositions that have been established in `verified_propositions` directory with "
                    "\\ref{path-without-extension}, where the path is relative to `verified_propositions` and subdirectories use backslashes, "
                    "for example \\ref{category\\filename}."
                ),
            ]
            if part
        )

    def _verifier_task(
        self,
        proposition_file: Path,
        *,
        workflow_index: int,
        attempt_index: int,
        attempt_total: int,
        config_name: str,
    ) -> str:
        rel = proposition_file.relative_to(self.layout.workspace_dir).as_posix()
        if config_name == "verifier_citation":
            review_instruction = (
                "Read the candidate proposition in `proposition.md` and perform only the citation/reference audit. "
                "Your final answer must include `Verdict: pass` or `Verdict: fail`. "
                "Check every `\\ref{...}` and every textual dependency claim: each valid citation must refer to an existing "
                "file in `verified_propositions` by path relative to `verified_propositions` without the `.md` extension. "
                "Subdirectories must be written with backslashes, such as `\\ref{category\\filename}`. The proposition must not cite, "
                "depend on, or present as established any proposition from `knowledge/`."
            )
        else:
            review_instruction = (
                "Read the candidate proposition in `proposition.md` and write a rigorous review of the statement and proof. "
                "Your final answer must include `Verdict: pass` or `Verdict: fail`. "
                "Focus on mathematical correctness, completeness, hidden assumptions, and logical rigor. "
                "A separate first verifier attempt audits `\\ref{...}` targets and `knowledge/` misuse."
            )
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Candidate Proposition File\n"
            + rel
            + "\n\n"
            + review_instruction
            + f"\n\nVerifier workflow: {workflow_index}\n"
            + f"Independent verification attempt: {attempt_index} of {attempt_total}\n"
            + f"Verifier config: {config_name}"
        )

    def _review_verdict_task(self, review_text: str, *, workflow_index: int, attempt_index: int) -> str:
        return (
            f"# Verifier Workflow\n{workflow_index}\n\n"
            f"# Attempt\n{attempt_index}\n\n"
            "# Attempt Review\n"
            + review_text
            + "\nReturn exactly either `pass` or `fail`."
        )

    def _theorem_checker_task(self, verified_file: Path, *, attempt_index: int) -> str:
        rel = verified_file.relative_to(self.layout.workspace_dir).as_posix()
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Newly Verified Proposition File\n"
            + rel
            + "\n\nDecide whether the newly verified proposition, together with any verified propositions cited by "
            "`\\ref{path-without-extension}`, proves the original problem. The path is relative to `verified_propositions` "
            "and subdirectories use backslashes, such as `\\ref{category\\filename}`. Read cited verified propositions as needed. "
            "Do not re-review the proposition proof except to understand what has been established. Your final answer must "
            "include exactly one line `Solves original problem: yes` or `Solves original problem: no`."
            + f"\n\nIndependent theorem check attempt: {attempt_index} of {AlphaSolveConfig.CHECK_IS_THEOREM_TIMES}"
        )

    def _reviser_task(self, proposition_file: Path, review_text: str, *, workflow_index: int) -> str:
        rel = proposition_file.relative_to(self.layout.workspace_dir).as_posix()
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Candidate Proposition File\n"
            + rel
            + "\n\n# Review\n"
            + review_text
            + "\n\nRewrite the same proposition markdown file in place, addressing every review issue."
            + f"\n\nRevision after verifier workflow: {workflow_index}"
        )

    def _finish(
        self,
        status: str,
        summary: str,
        *,
        proposition_file: Path | None = None,
        verified_file: Path | None = None,
        review_file: Path | None = None,
        theorem_check_file: Path | None = None,
        solved_problem: bool = False,
    ) -> WorkerRunResult:
        self._set_phase("done", status=status)
        trace_path = self.worker_dir / "trace.json"
        trace_path.write_text(json.dumps(self.trace, ensure_ascii=False, indent=2), encoding="utf-8")
        return WorkerRunResult(
            worker_id=self.worker_id,
            worker_dir=self.worker_dir,
            status=status,
            summary=summary,
            proposition_file=proposition_file,
            verified_file=verified_file,
            review_file=review_file,
            theorem_check_file=theorem_check_file,
            solved_problem=solved_problem,
            trace=list(self.trace),
        )

    def _event_sink(self, role: str):
        return compose_event_sinks(
            make_worker_event_sink(self.renderer, worker_id=self.worker_id, role=role),
            self._worker_log_sink,
        )

    def _generator_event_sink(self, curator_context: GeneratorCuratorContext):
        worker_sink = self._event_sink("generator")

        def sink(event: dict[str, Any]) -> None:
            curator_context.record_event(event)
            if worker_sink is not None:
                worker_sink(event)

        return sink

    def _set_phase(self, phase: str, *, status: str, model: str = "") -> None:
        if self.progress_callback is not None:
            self.progress_callback(phase, status)
        if self.renderer is not None:
            self.renderer.clear_worker_text(self.worker_id)
            if model:
                self.renderer.set_worker_model(self.worker_id, model)
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

    def _verifier_attempt_config(self, config_name: str) -> GeneralAgentConfig:
        config = self.suite.agents[config_name]
        tools = [name for name in config.tools if name not in {"Write", "Edit"}]
        if tools == config.tools:
            return config
        return replace(config, tools=tools)

    def _model_name(self, config: GeneralAgentConfig) -> str:
        ref = str(config.model_config or "").strip()
        if not ref:
            return ""
        if ref in self.suite.models:
            return str(self.suite.models[ref].get("model", ref))
        preset = ref.upper()
        if not preset.endswith("_CONFIG"):
            preset += "_CONFIG"
        cfg = getattr(AlphaSolveConfig, preset, None)
        if isinstance(cfg, dict):
            return str(cfg.get("model", ref))
        return ref


def _parse_review_verdict(text: str) -> str:
    clean = (text or "").strip().lower()
    if "pass" in clean and "fail" not in clean:
        return "pass"
    if "fail" in clean and "pass" not in clean:
        return "fail"
    return "fail"


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
