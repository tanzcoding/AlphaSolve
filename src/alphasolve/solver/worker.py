from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import AgentConfig, Agent, AgentEventSink, Workspace
from alphasolve.solver.wolfram_state import AlphaSolveConfig
from alphasolve.llm.types import Message
from alphasolve.solver.logging.event_log import compose_event_sinks
from alphasolve.solver.ui.dashboard import make_worker_event_sink
from .project import ProjectLayout
from .client_factory import ClientFactory
from .difficulty_declaration import materialize_difficulty_handoff
from .role import Role, RoleContext
from .tool_runtime import build_solver_tool_registry
from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from alphasolve.solver.execution import ExecutionGateway
    from alphasolve.solver.logging.log_session import LogSession
    from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer
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


_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


_RESULT_SUMMARY_PROMPT = _load_prompt("result_summarizer")


_REMOVE_RETRY_DELAYS = (0.1, 0.3, 0.7)
_EXTERNAL_YEAR_RE = re.compile(r"(?<![A-Za-z0-9])(?:1[7-9]\d{2}|20\d{2}|2100)(?![A-Za-z0-9])")


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


def _external_reference_parentheticals(text: str) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for match in _EXTERNAL_YEAR_RE.finditer(text):
        year_start = match.start()
        year_end = match.end()
        left = text.rfind("(", max(0, year_start - 50), year_start)
        if left == -1:
            continue
        depth = 0
        right = -1
        for index in range(left, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    right = index
                    break
                if depth < 0:
                    break
        if right == -1 or right < year_end or right - year_end > 50:
            continue
        parenthetical = " ".join(text[left + 1:right].split())
        line_number = text.count("\n", 0, left) + 1
        key = (line_number, parenthetical)
        if parenthetical and key not in seen:
            seen.add(key)
            results.append(key)
    return results


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
    direction_id: str | None = None
    gap_id: str | None = None
    solved_problem: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)
    # --- consolidation worker 的结构化反馈 ---
    is_consolidation: bool = False
    # None = 非 consolidation worker 或尚不确定；True = 完成了 pinned_target；False = 未完成
    target_achieved: bool | None = None
    # 每轮 verifier 的 (verdict, review 摘要)，rejected 时供 orchestrator 判断失败模式
    verify_history: list[dict[str, Any]] = field(default_factory=list)
    # 回传 hint 和 pinned_target，便于 orchestrator 对比"要求什么" vs "拿到了什么"
    worker_hint: str | None = None
    pinned_target: str | None = None
    # worker 完成后的统一 LLM 结果总结文件路径（result_summary.md）。verified/rejected
    # 共用同一份文件和同一份 prompt，内部已经核对过 generator 的原始困难声明是否过时。
    result_summary_file: Path | None = None
    # generator 产出的困难声明文件路径（difficulty_declaration.md）
    # 记录 worker 回避了什么数学困难、为什么回避、尝试过但失败的路线
    difficulty_declaration_file: Path | None = None
    # worker 结束后根据最终修订/审查轨迹生成的结构化困难交接卡片。
    # 它是组合比较候选，不是已确认的 persistent blocker。
    difficulty_handoff_file: Path | None = None
    difficulty_handoff: dict[str, Any] | None = None
    # 结构化失败分类，避免把协议错误、无候选和数学验证失败混为一谈。
    failure_kind: str | None = None
    blocking_obligation: str | None = None
    method_id: str | None = None
    # orchestrator 下发的验收清单，回传供 orchestrator 做结构化反思
    rubric: str | None = None


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
        direction_id: str | None = None,
        gap_id: str | None = None,
        method_id: str | None = None,
        frontier_refs: list[str] | None = None,
        frontier_note: str | None = None,
        is_free: bool = False,
        allow_weakening: bool = True,
        pinned_target: str | None = None,
        rubric: str | None = None,
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
        self.direction_id = (direction_id or "").strip() or None
        self.gap_id = (gap_id or "").strip() or None
        self.method_id = (method_id or "direct_proof").strip() or "direct_proof"
        self.frontier_refs = list(frontier_refs) if frontier_refs else []
        self.frontier_note = frontier_note
        self.is_free = bool(is_free)
        # 兑现/对偶环境开关：allow_weakening=False 时，禁止 generator/reviser 把命题
        # 弱化成"勉强能证"的子命题（关掉 reviser 的 Weakening / Isolating 两个动作），
        # 只允许「按原样证目标」或「给出显式见证证否(refute)」。pinned_target 为钉死的
        # 目标命题文本（缺省用 worker_hint / problem 表达的目标）。默认 True 完全向后兼容。
        self.allow_weakening = bool(allow_weakening)
        self.pinned_target = (pinned_target or "").strip() or None
        self.rubric = (rubric or "").strip() or None
        # consolidation worker = 禁止弱化的正面强攻 worker（allow_weakening=False）
        self.is_consolidation = not self.allow_weakening
        # global_attack: 全局 consolidation，pinned_target = problem.md 全文。
        # 由 orchestrator 通过 consolidation=true + global_attack 隐式触发（pinned_target
        # 内容为 problem 全文时自动识别），用于在 prompt 中强调"攻击 problem 本身而非子方向"。
        self.is_global_attack = bool(
            self.is_consolidation
            and self.pinned_target
            and "global-problem-attack" in (direction_id or "")
        )
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
        self._role_ctx = RoleContext(
            workspace=self.workspace,
            worker_rel=self.worker_rel,
            worker_dir=self.worker_dir,
            suite=self.suite,
            client_factory=self.client_factory,
            subagent_max_depth=self.subagent_max_depth,
            execution_gateway=self.execution_gateway,
            curator_queue=self.curator_queue,
            log_session=self.log_session,
            stop_event=self.stop_event,
            event_sink_factory=self._event_sink,
            trace=self.trace,
        )

    def run(self) -> WorkerRunResult:
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        self._set_phase("starting", status="running")
        if self._should_stop():
            return self._finish("cancelled", "worker cancelled because another worker solved the problem")
        if self.worker_hint:
            (self.worker_dir / "worker_hint.md").write_text(self.worker_hint, encoding="utf-8")
        (self.worker_dir / "research_target.json").write_text(
            json.dumps(
                {
                    "worker_id": self.worker_id,
                    "direction_id": self.direction_id,
                    "gap_id": self.gap_id,
                    "method_id": self.method_id,
                    "hint": self.worker_hint,
                    "is_free_exploration": self.is_free,
                    "allow_weakening": self.allow_weakening,
                    "pinned_target": self.pinned_target,
                    "rubric": self.rubric,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        # 落盘 orchestrator 下发的精选前沿（或 free 探索指引+发散种子），便于日志观测。
        frontier_text = self._render_frontier()
        if frontier_text:
            frontier_name = "free_exploration.md" if self.is_free else "curated_frontier.md"
            (self.worker_dir / frontier_name).write_text(frontier_text, encoding="utf-8")

        try:
            try:
                proposition_file = self._run_generator(frontier_text)
                if self._should_stop():
                    return self._finish(
                        "cancelled",
                        "worker cancelled because another worker solved the problem",
                        proposition_file=proposition_file,
                    )
                if proposition_file is None:
                    return self._finish(
                        "rejected", "generator did not produce the required proposition.md file",
                        target_achieved=False if self.is_consolidation else None,
                        failure_kind="generator_protocol_failure",
                        blocking_obligation="Generator completed without a valid proposition.md output.",
                    )
                protocol_error = self._validate_proposition_file(proposition_file)
                if protocol_error:
                    return self._finish(
                        "rejected",
                        f"generator produced an invalid proposition.md: {protocol_error}",
                        proposition_file=proposition_file,
                        target_achieved=False if self.is_consolidation else None,
                        failure_kind="generator_protocol_failure",
                        blocking_obligation=protocol_error,
                    )
                self._snapshot_proposition(proposition_file, version=0)

                final_review_file: Path | None = None
                last_review_text = ""
                verify_history: list[dict[str, Any]] = []
                for workflow_index in range(1, self.max_verify_rounds + 1):
                    if self._should_stop():
                        return self._finish(
                            "cancelled",
                            "worker cancelled because another worker solved the problem",
                            proposition_file=proposition_file,
                            verify_history=verify_history,
                        )
                    workflow_result = self._run_verifier_workflow(proposition_file, workflow_index=workflow_index)
                    last_review_text = workflow_result.review_text
                    final_review_file = workflow_result.review_file
                    verify_history.append({
                        "round": workflow_index,
                        "verdict": "pass" if workflow_result.passed else "fail",
                        "review_excerpt": (workflow_result.review_text or "")[:1200],
                    })
                    if self._should_stop():
                        return self._finish(
                            "cancelled",
                            "worker cancelled because another worker solved the problem",
                            proposition_file=proposition_file,
                            verify_history=verify_history,
                        )
                    if workflow_result.passed:
                        if self._should_stop():
                            return self._finish(
                                "cancelled",
                                "worker cancelled because another worker solved the problem",
                                proposition_file=proposition_file,
                                verify_history=verify_history,
                            )
                        verified = self._copy_to_verified(proposition_file)
                        solved_problem, theorem_check_text = self._run_theorem_checks(verified)
                        theorem_check_file = self.worker_dir / "theorem_check.md"
                        theorem_check_file.write_text(theorem_check_text, encoding="utf-8")
                        # 成功时也跑统一结果总结器：让 orchestrator 感知 Statement 是否被弱化
                        result_summary_file = self._run_result_summarizer(
                            status="verified",
                            proposition_file=proposition_file,
                            review_file=final_review_file,
                            verify_history=verify_history,
                        )
                        # consolidation worker: verifier 通过不等于完成了 pinned_target
                        # 代码不做 LLM 判断，只标记 verified；orchestrator 负责判断 target_achieved
                        target_achieved = None if self.is_consolidation else True
                        difficulty_decl = self.worker_dir / "difficulty_declaration.md"
                        return self._finish(
                            "verified",
                            "Successfully produced a verified proposition. Statement: "
                            + _extract_statement(proposition_file.read_text(encoding="utf-8")),
                            proposition_file=proposition_file,
                            verified_file=verified,
                            review_file=final_review_file,
                            theorem_check_file=theorem_check_file,
                            solved_problem=solved_problem,
                            verify_history=verify_history,
                            target_achieved=target_achieved,
                            result_summary_file=result_summary_file,
                            difficulty_declaration_file=difficulty_decl if difficulty_decl.exists() else None,
                        )
                    if workflow_index < self.max_verify_rounds:
                        self._run_reviser(proposition_file, workflow_result.review_text, workflow_index=workflow_index)
                        if self._should_stop():
                            return self._finish(
                                "cancelled",
                                "worker cancelled because another worker solved the problem",
                                proposition_file=proposition_file,
                                verify_history=verify_history,
                            )

                summary = "Failed to produce a verified proposition."
                if proposition_file.exists():
                    summary += "\n\n" + proposition_file.read_text(encoding="utf-8")[:4000]
                if last_review_text and (final_review_file is None or not final_review_file.exists()):
                    final_review_file = self._write_final_review(last_review_text)
                if final_review_file is not None and final_review_file.exists():
                    summary += "\n\nFinal review:\n" + final_review_file.read_text(encoding="utf-8")[:4000]
                # consolidation worker rejected = 明确未完成目标
                target_achieved = False if self.is_consolidation else None
                # 统一结果总结器：供 orchestrator 快速诊断失败模式
                result_summary_file = self._run_result_summarizer(
                    status="rejected",
                    proposition_file=proposition_file,
                    review_file=final_review_file,
                    verify_history=verify_history,
                )
                difficulty_decl = self.worker_dir / "difficulty_declaration.md"
                return self._finish(
                    "rejected", summary,
                    proposition_file=proposition_file,
                    review_file=final_review_file,
                    verify_history=verify_history,
                    target_achieved=target_achieved,
                    result_summary_file=result_summary_file,
                    difficulty_declaration_file=difficulty_decl if difficulty_decl.exists() else None,
                    failure_kind="verification_rejected",
                    blocking_obligation=(last_review_text or "Verifier rejected the candidate proposition.")[:2000],
                )
            except Exception as exc:
                return self._finish("failed", str(exc), failure_kind="execution_failed")
        finally:
            if self._worker_log_sink is not None:
                self._worker_log_sink.close()

    def _run_generator(self, frontier_text: str | None = None) -> Path | None:
        config = self.suite.agents["generator"]
        self._set_phase("generator", status="thinking", model=self._model_name(config))
        curator_context = GeneratorCuratorContext(worker_id=self.worker_id, worker_rel=self.worker_rel)

        def event_sink_decorator(base: AgentEventSink | None) -> AgentEventSink | None:
            def sink(event: dict[str, Any]) -> None:
                curator_context.record_event(event)
                if base is not None:
                    base(event)
            return sink

        role = Role.for_generator(
            self._role_ctx,
            curator_context_provider=curator_context.consume,
            event_sink_decorator=event_sink_decorator,
        )
        role.run(self._generator_task(frontier_text))
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
            self._snapshot_review(
                last_review_text,
                workflow=workflow_index,
                attempt=attempt_index,
                config=config_name,
            )
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
        phase_label = f"verifier_attempt w{workflow_index}.{attempt_index}"
        config = self._verifier_attempt_config(config_name)
        self._set_phase(phase_label, status="thinking", model=self._model_name(config))
        role = Role.for_verifier_attempt(
            self._role_ctx,
            config=config,
            config_name=config_name,
            workflow_index=workflow_index,
            attempt_index=attempt_index,
        )
        result = role.run(
            self._verifier_task(
                proposition_file,
                workflow_index=workflow_index,
                attempt_index=attempt_index,
                attempt_total=self.verifier_scaling_factor,
                config_name=config_name,
            )
        )
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
        config = self.suite.agents["theorem_checker"]
        self._set_phase("theorem_checker", status="thinking", model=self._model_name(config))
        role = Role.for_theorem_checker(self._role_ctx, attempt_index=attempt_index)
        result = role.run(self._theorem_checker_task(verified_file, attempt_index=attempt_index))
        return result.final_answer

    def _run_reviser(self, proposition_file: Path, review_text: str, *, workflow_index: int) -> None:
        config = self.suite.agents["reviser"]
        self._set_phase(f"reviser w{workflow_index}", status="thinking", model=self._model_name(config))
        exact_rel = proposition_file.relative_to(self.layout.workspace_dir).as_posix()
        role = Role.for_reviser(
            self._role_ctx,
            proposition_rel=exact_rel,
            workflow_index=workflow_index,
        )
        role.run(self._reviser_task(proposition_file, review_text, workflow_index=workflow_index))
        if proposition_file.is_file():
            self._snapshot_proposition(proposition_file, version=workflow_index)

    def _run_review_verdict_judge(self, review_text: str, *, workflow_index: int, attempt_index: int) -> str:
        role = f"review_verdict_judge w{workflow_index}.{attempt_index}"
        base_config = self.suite.agents.get("verifier") or self.suite.agents[self._verifier_config_names()[0]]
        config = AgentConfig(
            name="review_verdict_judge",
            system_prompt=_REVIEW_VERDICT_PROMPT,
            tools=(),
            max_turns=base_config.max_turns,
            tier=base_config.tier,
        )
        self._set_phase(role, status="thinking", model=self._model_name(config))
        agent = Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_solver_tool_registry(
                RoleWorkspaceAccess.worker_read_only(self.workspace, self.worker_rel),
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

    def _run_result_summarizer(
        self,
        *,
        status: str,
        proposition_file: Path | None,
        review_file: Path | None,
        verify_history: list[dict[str, Any]],
    ) -> Path | None:
        """worker 结束后的统一 LLM 结果总结：verified/rejected 共用同一份 prompt 和输出文件。

        取代原来互斥的 outcome_summarizer（仅 verified）/ failure_summarizer（仅 rejected）——
        两者本来就只会二选一执行，合并后调用次数不变，只是不再维护两份几乎重复的 prompt，
        也不再让 orchestrator 按 status 分别去读两个不同的文件名。

        生成 result_summary.md 并返回其路径；若 LLM 调用失败则返回 None（不影响主流程）。
        """
        if self._should_stop():
            return None
        try:
            base_config = self.suite.agents.get("verifier") or self.suite.agents[self._verifier_config_names()[0]]
            config = AgentConfig(
                name="result_summarizer",
                system_prompt=_RESULT_SUMMARY_PROMPT,
                tools=(),
                max_turns=base_config.max_turns,
                tier=base_config.tier,
            )
            self._set_phase("result_summarizer", status="thinking", model=self._model_name(config))
            agent = Agent(
                config=config,
                client=self.client_factory(config),
                tool_registry=build_solver_tool_registry(
                    RoleWorkspaceAccess.worker_read_only(self.workspace, self.worker_rel),
                ),
                event_sink=self._event_sink("result_summarizer"),
                stop_event=self.stop_event,
            )
            task = self._result_summary_task(
                status=status,
                proposition_file=proposition_file,
                review_file=review_file,
                verify_history=verify_history,
            )
            result = agent.run(task)
            summary_text = (result.final_answer or "").strip()
            self.trace.append({
                "role": "result_summarizer",
                "final_answer": summary_text[:2000],
            })
            # 完整结果写入 worker log sink（实时可观测）
            if self._worker_log_sink is not None:
                self._worker_log_sink({
                    "type": "message",
                    "role": "result_summarizer",
                    "content": summary_text,
                })
            if not summary_text:
                return None
            result_summary_file = self.worker_dir / "result_summary.md"
            result_summary_file.write_text(summary_text, encoding="utf-8")
            return result_summary_file
        except Exception:
            return None

    def _result_summary_task(
        self,
        *,
        status: str,
        proposition_file: Path | None,
        review_file: Path | None,
        verify_history: list[dict[str, Any]],
    ) -> str:
        """组装统一结果总结器的输入：状态 + 完整修订轨迹 (statement, proof, review) × N 轮
        + generator 初始困难声明与 reviser 更新（如有）。

        从 revision_history/ 读取每轮 proposition 快照 + 配对 review，按时间线排列；
        另把 difficulty_declaration.md（若存在）当作一段材料喂进去——它保留 generator 的初始
        声明并追加 reviser 的更新。让总结器核对每个障碍是否仍成立，而不是让 orchestrator
        事后再去读一份可能过时的独立文件。
        """
        parts: list[str] = [f"## Outcome Status\n\n{status}"]
        if self.worker_hint:
            parts.append(f"## Worker Hint\n\n{self.worker_hint[:2000]}")
        if self.pinned_target:
            parts.append(f"## Pinned Target\n\n{self.pinned_target[:1500]}")
        difficulty_decl = self.worker_dir / "difficulty_declaration.md"
        if difficulty_decl.is_file():
            try:
                decl_text = difficulty_decl.read_text(encoding="utf-8")[:3000]
            except OSError:
                decl_text = ""
            if decl_text:
                parts.append(
                    "## Generator's Original Difficulty Declaration (written before revision — "
                    "may be stale; reconcile against the trail below)\n\n" + decl_text
                )

        # 从 revision_history/ 构建完整轨迹
        history_dir = self.worker_dir / "revision_history"
        trail_entries: list[dict[str, Any]] = []
        if history_dir.is_dir():
            # 收集所有 proposition 快照
            prop_files = sorted(history_dir.glob("proposition.v*.md"))
            for pf in prop_files:
                version = pf.stem.replace("proposition.v", "")
                try:
                    prop_text = pf.read_text(encoding="utf-8")
                except OSError:
                    continue
                # 提取 Statement
                stmt = ""
                in_stmt = False
                for line in prop_text.split("\n"):
                    if line.strip().startswith("## Statement"):
                        in_stmt = True
                        continue
                    if in_stmt and line.strip().startswith("##"):
                        break
                    if in_stmt:
                        stmt += line + "\n"
                stmt = stmt.strip()[:600]

                trail_entries.append({
                    "version": version,
                    "type": "proposition",
                    "statement": stmt,
                    "proof_preview": prop_text[:2000],
                })

            # 收集所有 review 快照
            review_files = sorted(history_dir.glob("review.w*_a*_*.md"))
            for rf in review_files:
                try:
                    review_text = rf.read_text(encoding="utf-8")
                except OSError:
                    continue
                # 提取 verdict
                verdict = "unknown"
                for line in review_text.split("\n"):
                    low = line.strip().lower()
                    if "verdict" in low:
                        verdict = "pass" if "pass" in low else ("fail" if "fail" in low else "unknown")
                        break
                trail_entries.append({
                    "version": rf.stem,
                    "type": "review",
                    "verdict": verdict,
                    "review_preview": review_text[:2000],
                })

        if trail_entries:
            parts.append("## Full Revision Trail\n")
            for entry in trail_entries:
                if entry["type"] == "proposition":
                    parts.append(
                        f"### Round v{entry['version']} — Proposition\n"
                        f"**Statement:** {entry['statement']}\n\n"
                        f"**Proof (preview):**\n{entry['proof_preview']}\n"
                    )
                else:
                    parts.append(
                        f"### {entry['version']} — Review (verdict: {entry['verdict']})\n"
                        f"{entry['review_preview']}\n"
                    )
            trail_text = "\n".join(parts)
        else:
            # 回退：没有 revision_history 时用旧的逻辑
            if proposition_file and proposition_file.exists():
                parts.append(f"## Final Proposition\n\n{proposition_file.read_text(encoding='utf-8')[:4000]}")
            if review_file and review_file.exists():
                parts.append(f"## Final Review\n\n{review_file.read_text(encoding='utf-8')[:3000]}")
            if verify_history:
                vh_lines = []
                for vh in verify_history:
                    vh_lines.append(f"- Round {vh.get('round', '?')}: verdict={vh.get('verdict', '?')} — {(vh.get('review_excerpt') or '')[:500]}")
                parts.append("## Verify History (all rounds failed)\n\n" + "\n".join(vh_lines))
            trail_text = "\n\n".join(parts)

        return trail_text

    def _summarize_trace_for_failure(self) -> str:
        """从 trace 中提取关键步骤摘要，供 failure_summarizer 参考。"""
        role_counts: dict[str, int] = {}
        for entry in self.trace:
            role = str(entry.get("role") or entry.get("type") or "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1
        if not role_counts:
            return ""
        lines = [f"- {role}: {count} call(s)" for role, count in role_counts.items()]
        return "Agent call breakdown:\n" + "\n".join(lines)

    def _summarize_trace_for_failure(self) -> str:
        """从 trace 中提取关键步骤摘要，供 failure_summarizer 参考。"""
        role_counts: dict[str, int] = {}
        for entry in self.trace:
            role = str(entry.get("role") or entry.get("type") or "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1
        if not role_counts:
            return ""
        lines = [f"- {role}: {count} call(s)" for role, count in role_counts.items()]
        return "Agent call breakdown:\n" + "\n".join(lines)

    def _find_proposition_file(self) -> Path | None:
        """严格兑现 generator 的固定输出协议，绝不把 frontier/guidance 当作命题。"""
        candidate = self.worker_dir / "proposition.md"
        return candidate if candidate.is_file() else None

    @staticmethod
    def _validate_proposition_file(path: Path) -> str | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"cannot read proposition.md: {exc}"
        if not text.strip():
            return "proposition.md is empty"
        headings = re.findall(r"(?m)^##\s+(.+?)\s*$", text)
        if [item.strip().lower() for item in headings] != ["statement", "proof"]:
            return "proposition.md must contain exactly ## Statement followed by ## Proof"
        statement_match = re.search(r"(?ms)^##\s+Statement\s*$\s*(.*?)^##\s+Proof\s*$", text, flags=re.IGNORECASE)
        proof_match = re.search(r"(?ms)^##\s+Proof\s*$\s*(.*)\Z", text, flags=re.IGNORECASE)
        if statement_match is None or not statement_match.group(1).strip():
            return "the Statement section is empty"
        if proof_match is None or not proof_match.group(1).strip():
            return "the Proof section is empty"
        return None

    def _snapshot_proposition(self, proposition_file: Path, *, version: int) -> Path:
        history_dir = self.worker_dir / "revision_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        target = history_dir / f"proposition.v{version}.md"
        shutil.copy2(proposition_file, target)
        return target

    def _snapshot_review(self, review_text: str, *, workflow: int, attempt: int, config: str) -> Path:
        """将单次 verifier attempt 的审查结果存入 revision_history，与 proposition 快照配对。"""
        history_dir = self.worker_dir / "revision_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        target = history_dir / f"review.w{workflow}_a{attempt}_{config}.md"
        target.write_text(review_text, encoding="utf-8")
        return target

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
            "Read the following verified mathematical proposition and return a descriptive kebab-case filename "
            "(5-15 words, lowercase, hyphens only, no extension) that exactly captures its mathematical content. "
            "Examples: parity-obstruction-for-even-sum-of-two-odd-integers, "
            "matrix-rank-bound-under-product-nullspace-containment, "
            "convexity-extremal-case-for-affine-function-on-compact-polytope, "
            "orbit-counting-invariant-for-finite-group-action-on-colored-sets. "
            "Return ONLY the filename, nothing else.\n\n"
            + content
        )
        try:
            client = self.client_factory(config)
            response = client.complete(
                messages=[Message(role="user", content=prompt)],
                tools=[],
            )
            raw = (response.message.content or "").strip().lower()
            name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
            name = re.sub(r"-{2,}", "-", name)
            if name and len(name) <= 140:
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
            (self.worker_dir / "research_target.json").resolve(),
            (self.worker_dir / "difficulty_declaration.md").resolve(),
            (self.worker_dir / "revision_history").resolve(),
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

    def _pinned_target_block(self) -> str:
        """兑现/对偶环境的钉死目标说明（allow_weakening=False 时注入）。

        普通 worker（allow_weakening=True）返回空串，行为与改动前完全一致。
        """
        if self.allow_weakening:
            return ""
        target = self.pinned_target or (self.worker_hint or "").strip()
        parts = [
            "# Pinned Target (no weakening allowed)",
            (
                "This is a CONSOLIDATION / DUAL attempt on a FIXED target. You must NOT weaken, "
                "narrow, or add fresh hypotheses to make the statement 'barely provable', and you "
                "must NOT isolate a smaller sub-claim and emit that instead. Only two outcomes count "
                "as success:\n"
                "  (1) Prove the pinned target EXACTLY as stated, using only already-verified "
                "propositions (via \\ref{...}) plus rigorous reasoning; or\n"
                "  (2) REFUTE it with an explicit, independently checkable witness (a concrete "
                "permutation / construction / tiling) and prove the witness meets every stated "
                "condition — emit that refuting statement as the proposition.\n"
                "If you can do neither, keep the pinned Statement unchanged. In `## Proof`, present only "
                "the honest partial derivation and explicitly name the single blocking obligation. Do NOT "
                "replace the Statement with the largest fragment you proved: that would be weakening. The "
                "verifier should reject the incomplete pinned proof, allowing the orchestrator to route the gap. "
                "Introducing an unproven assumption to bridge the gap is a failure, not a success."
            ),
        ]
        if self.is_global_attack:
            parts.append(
                "This is a GLOBAL ATTACK on the full problem itself (`problem.md`), not a sub-direction's "
                "terminal goal. The pinned target below is the complete problem statement. You must aim to "
                "resolve the entire problem — do not prove a sub-claim, a special case, or a necessary "
                "condition and present it as the answer. Use the accumulated verified propositions as "
                "building blocks; the orchestrator judged that enough material has accumulated to warrant a "
                "head-on attempt on the full problem."
            )
        if target:
            parts.append("The pinned target is:\n\n" + target)
        return "\n\n".join(parts)

    def _scan_failed_history(self, max_results: int = 3) -> str:
        """扫描历史上失败的 worker，提取命题和失败原因，帮助当前 worker 避免踩坑。

        在 generator 和 reviser 的 task prompt 中注入，让 worker 在开始干活前
        就知道前人试过什么、卡在哪里。只扫描有 revision_history/ 的 worker，
        按 hint 文本相似度排序取 top-N。
        """
        unverified = self.layout.unverified_dir
        if not unverified.is_dir():
            return ""
        hint_words = set((self.worker_hint or "").lower().split())
        if not hint_words:
            return ""

        scored: list[tuple[int, Path]] = []
        for prop_dir in sorted(unverified.iterdir()):
            if not prop_dir.is_dir() or not prop_dir.name.startswith("prop-"):
                continue
            history_dir = prop_dir / "revision_history"
            if not history_dir.is_dir():
                continue
            # 只取有 proposition 快照的
            versions = sorted(history_dir.glob("proposition.v*.md"))
            if not versions:
                continue
            # 用 worker_hint.md 或 research_target.json 提取原始 hint
            target_json = prop_dir / "research_target.json"
            hint_text = ""
            if target_json.is_file():
                try:
                    data = json.loads(target_json.read_text(encoding="utf-8"))
                    hint_text = (data.get("hint") or data.get("pinned_target") or "")
                except (json.JSONDecodeError, OSError):
                    pass
            if not hint_text:
                hint_file = prop_dir / "worker_hint.md"
                if hint_file.is_file():
                    hint_text = hint_file.read_text(encoding="utf-8")
            # 相似度：hint 关键词命中数
            target_words = set(hint_text.lower().split())
            score = len(hint_words & target_words)
            if score > 0:
                scored.append((score, prop_dir))

        scored.sort(key=lambda x: -x[0])
        if not scored:
            return ""

        parts: list[str] = [
            "# Historical Failure Warnings",
            (
                "The following are past worker attempts on tasks similar to yours. "
                "Each entry shows what they tried to prove, how many revision rounds they went through, "
                "and what the verifier rejected them for. Read these BEFORE writing your own proposition "
                "to avoid repeating known dead ends."
            ),
        ]
        for _, prop_dir in scored[:max_results]:
            history_dir = prop_dir / "revision_history"
            versions = sorted(history_dir.glob("proposition.v*.md"))
            # 提取每个版本的 Statement 首句
            stmt_lines: list[str] = []
            for vf in versions:
                text = vf.read_text(encoding="utf-8")
                # 找 "## Statement" 后的第一段非空文本
                in_stmt = False
                for line in text.split("\n"):
                    if line.strip().startswith("## Statement"):
                        in_stmt = True
                        continue
                    if in_stmt and line.strip().startswith("##"):
                        break
                    if in_stmt and line.strip():
                        stmt_lines.append(f"  v{vf.stem.replace('proposition.v', '')}: {line.strip()[:200]}")
                        break

            # 提取 review 摘要
            reviews = sorted(history_dir.glob("review.*.md"))
            review_summary = ""
            if reviews:
                last_review = reviews[-1].read_text(encoding="utf-8")
                # 找 "Verdict" 或 "fail" 相关行
                for line in last_review.split("\n"):
                    low = line.strip().lower()
                    if "verdict" in low or "fail" in low or "gap" in low or "incomplete" in low:
                        review_summary = line.strip()[:200]
                        break
                if not review_summary:
                    review_summary = last_review.strip()[:200]

            parts.append(
                f"\n### Worker `{prop_dir.name}` — {len(versions)} revision rounds\n"
                + "\n".join(stmt_lines)
                + (f"\n  Last review: {review_summary}" if review_summary else "")
            )

        return "\n".join(parts)

    def _generator_task(self, frontier: str | None = None) -> str:
        if frontier is None:
            frontier = self._render_frontier()
        frontier_header = "# Free Exploration Guidance" if self.is_free else "# Curated Frontier"
        pinned = self._pinned_target_block()
        failed_history = self._scan_failed_history(max_results=3)
        return "\n\n".join(
            part
            for part in [
                "# Problem",
                self.layout.read_problem(),
                "# General Hint",
                self.layout.read_hint(),
                "# Task Guidance",
                self.worker_hint,
                f"# Assigned Method\n{self.method_id}",
                pinned,
                frontier_header if frontier else "",
                frontier,
                failed_history,
                "# Output",
                (
                    "Create a file named `proposition.md` directly in your own directory "
                    f"`{self.worker_rel}`. The file must contain "
                    "exactly two sections, `## Statement` and `## Proof`, with no remarks or extra headings. "
                    "The statement must be a pure mathematical statement, not a lemma/proposition/theorem-labeled block. "
                    "You may reference verified propositions that have been established in `verified_propositions` directory with "
                    "\\ref{path-without-extension}, where the path is relative to `verified_propositions` and subdirectories use backslashes, "
                    "for example \\ref{category\\filename}."
                ),
            ]
            if part
        )

    def _render_ref_statements(self, refs: list[str]) -> list[str]:
        """把一组 verified prop 引用展开为「\\ref + Statement」行（无损，只取陈述）。"""
        out: list[str] = []
        for ref in refs:
            rel = str(ref).replace("\\", "/").strip()
            if rel.endswith(".md"):
                rel = rel[:-3]
            if not rel:
                continue
            cite = rel.replace("/", "\\")
            path = self.layout.verified_dir / (rel + ".md")
            if not path.is_file():
                out.append(f"- \\ref{{{cite}}} (referenced file not found)")
                continue
            try:
                statement = _extract_statement(path.read_text(encoding="utf-8")).strip()
            except OSError:
                statement = ""
            out.append(f"- \\ref{{{cite}}}:\n{statement}" if statement else f"- \\ref{{{cite}}}")
        return out

    def _render_frontier(self) -> str:
        """组装注入 generator 任务的前沿/自由探索段（无损，只取陈述）。

        - free worker：始终注入“避开主流战略叙事”的指令；若有发散种子，则作为
          可选正交火种附上（明确"不要求在其上构建"）。
        - consolidation / global_attack worker：frontier 降级为可选参考上下文，
          不要求 worker 限定于这些命题的方法框架。
        - 非 free / 非 consolidation worker：orchestrator 下发了 refs/note 才注入，
          作为主上下文（curated frontier）。
        - 两者皆缺省内容时返回空串，行为与改动前一致（向后兼容）。
        """
        refs = self.frontier_refs
        note = (self.frontier_note or "").strip()
        if self.is_free:
            parts: list[str] = [
                "This is a FREE exploration slot. The attached constraints are mandatory: do not reuse a listed "
                "taboo method or active blocker under a new name. Choose one precise claim that is materially "
                "orthogonal to those routes, or record the exact reason no such claim is currently available."
            ]
            if note:
                parts.append(note)
            seeds = self._render_ref_statements(refs)
            if seeds:
                parts.append(
                    "Optional under-explored verified seed. Use it only if it remains consistent with the constraints; "
                    "it is not a requirement and does not itself establish a new direction:"
                )
                parts.extend(seeds)
            return "\n\n".join(parts)

        if not refs and not note:
            return ""
        if self.is_consolidation:
            parts = [
                "The orchestrator provided the following verified propositions as OPTIONAL background "
                "context for this CONSOLIDATION task. You are attacking a FIXED target with no weakening "
                "allowed. You are free to use ANY mathematical method — do NOT confine yourself to the "
                "methods or frameworks used in these refs. Read them for awareness, but choose your own "
                "attack path independently."
            ]
        else:
            parts = [
                "The orchestrator selected the following verified propositions as your primary frontier "
                "for this task. Prefer building on and citing these via \\ref{...}. Read additional verified "
                "propositions or unverified knowledge only when this frontier is clearly insufficient for the "
                "assigned mathematical task; do not turn that local reading into a workspace-wide strategy review."
            ]
        if note:
            parts.append(f"Orchestrator note: {note}")
        parts.extend(self._render_ref_statements(refs))

        # 自动注入 knowledge 脚手架提示：扫描 knowledge/ 目录中与 hint 关键词相关的
        # 半成品文件，提示 worker 可以参考但需独立验证。同时扫描已标注 INVALIDATED
        # 的死路，防止 worker 重复踩坑。
        scaffold_hint = self._scan_knowledge_scaffold()
        if scaffold_hint:
            parts.append(scaffold_hint)

        return "\n\n".join(parts)

    @staticmethod
    def _knowledge_title(content: str) -> str:
        """提取 Markdown 标题，跳过可选 YAML frontmatter。"""
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    lines = lines[index + 1 :]
                    break
        for line in lines:
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                if title:
                    return title[:80]
        return ""

    @staticmethod
    def _knowledge_is_invalidated(content: str) -> bool:
        """仅接受显式 frontmatter 状态，避免将含有历史反例的有效笔记整体禁用。"""
        match = re.match(r"\A---\s*\n(?P<frontmatter>.*?)\n---(?:\s*\n|\Z)", content, re.DOTALL)
        if match is None:
            return False
        return bool(
            re.search(
                r"(?mi)^(?:knowledge_)?status:\s*(?:invalidated|refuted|dead[_ -]?end)\s*$",
                match.group("frontmatter"),
            )
        )

    def _scan_knowledge_scaffold(self) -> str:
        """为 worker 提供相关的知识脚手架和显式标记的失效路线。"""
        knowledge_dir = self.workspace.root / "knowledge"
        if not knowledge_dir.is_dir():
            return ""

        keywords = set(re.findall(r"[a-z]{5,}", (self.worker_hint or "").lower()))
        if not keywords:
            return ""

        scaffolds: list[str] = []
        dead_ends: list[str] = []
        max_scaffolds = 5
        max_dead_ends = 5

        for root, dirs, files in os.walk(knowledge_dir):
            dirs.sort()
            for fname in sorted(files):
                if not fname.endswith(".md"):
                    continue
                fpath = Path(root) / fname
                try:
                    content = fpath.read_text(encoding="utf-8")[:8192]
                except (OSError, UnicodeDecodeError):
                    continue

                content_lower = content.lower()
                hits = sum(1 for keyword in sorted(keywords)[:20] if keyword in content_lower)
                rel_path = fpath.relative_to(knowledge_dir).as_posix()
                display_path = f"knowledge/{rel_path}"
                title = self._knowledge_title(content)

                if self._knowledge_is_invalidated(content):
                    if hits > 0 and len(dead_ends) < max_dead_ends:
                        dead_ends.append(f"  - `{display_path}`: {title}")
                    continue

                if hits >= 3 and len(scaffolds) < max_scaffolds:
                    scaffolds.append(f"  - `{display_path}`: {title}")

        parts: list[str] = []
        if scaffolds:
            parts.append(
                "The following knowledge notes contain semi-finished results relevant to your task. "
                "You may use them as proof scaffolding, but you MUST independently verify any claim "
                "before relying on it — knowledge notes are NOT verified propositions:"
            )
            parts.extend(scaffolds)

        if dead_ends:
            parts.append(
                "The following knowledge notes are explicitly marked invalidated or refuted for this "
                "topic (do NOT repeat their invalidated claim):"
            )
            parts.extend(dead_ends)

        return "\n".join(parts) if parts else ""

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
        if config_name == "verifier_format_references":
            review_instruction = (
                "Read the candidate proposition in `proposition.md` and perform the first format/reference-source gate. "
                "Your final answer must include `Verdict: pass` or `Verdict: fail`. "
                "Check that the file contains exactly two Markdown sections, `## Statement` followed by `## Proof`, "
                "with no other headings and no `remark` anywhere. The statement must be only a pure mathematical "
                "statement, not a lemma/proposition/theorem/claim-labeled block and not mixed with proof commentary. "
                "Also check whether the proposition cites, invokes, or relies on any paper, book, textbook, monograph, "
                "named author result, named external theorem, or similar external mathematical source that is not present "
                "under `knowledge/references/`. Fail if any such external result is absent from `knowledge/references/`."
            )
        elif config_name == "verifier_citation":
            possible_external_citations = _external_reference_parentheticals(proposition_file.read_text(encoding="utf-8"))
            external_citation_hint = ""
            if possible_external_citations:
                external_citation_hint = (
                    "\n\nPotential external paper/book citation parentheticals detected in `proposition.md`; "
                    "pay special attention to whether these are external-source dependencies rather than verified-proposition citations:\n"
                    + "\n".join(f"- line {line_number}: ({item})" for line_number, item in possible_external_citations)
                )
            review_instruction = (
                "Read the candidate proposition in `proposition.md` and perform the citation/reference audit, "
                "including whether each cited verified proposition is correctly applied. "
                "Your final answer must include `Verdict: pass` or `Verdict: fail`. "
                "Check every `\\ref{...}` and every textual dependency claim: each valid citation must refer to an existing "
                "file in `verified_propositions` by path relative to `verified_propositions` without the `.md` extension. "
                "Subdirectories must be written with backslashes, such as `\\ref{category\\filename}`. The proposition must not cite, "
                "depend on, or present as established any proposition from `knowledge/`. "
                "For each citation, read the cited verified proposition and delegate a `reasoning_subagent` to check "
                "whether the hypotheses and conditions of the cited proposition are satisfied in the context where "
                "the citation is used. Fail the proposition if any cited proposition's conditions are not met."
                + external_citation_hint
            )
        else:
            review_instruction = (
                "Read the candidate proposition in `proposition.md` and write a rigorous review of the statement and proof. "
                "Your final answer must include `Verdict: pass` or `Verdict: fail`. "
                "Focus on mathematical correctness, completeness, hidden assumptions, and logical rigor. "
                "Earlier verifier attempts audit file format, external-source admissibility, `\\ref{...}` targets, and `knowledge/` misuse."
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
            + "\n\nAssess this proposition against the original problem using the theorem-checker rules."
        )

    def _reviser_task(self, proposition_file: Path, review_text: str, *, workflow_index: int) -> str:
        rel = proposition_file.relative_to(self.layout.workspace_dir).as_posix()
        failed_history = self._scan_failed_history(max_results=3)
        no_weakening = ""
        if not self.allow_weakening:
            no_weakening = (
                "\n\n# Revision Constraint (no weakening)\n"
                "This revision runs under a PINNED target. Of the reviser's statement-change moves, "
                "ONLY 'Negating' (replace by a refutation backed by an explicit, checkable witness) is "
                "permitted. You must NOT 'Weaken' the statement and must NOT 'Isolate a sub-claim' to "
                "salvage a verified-but-smaller proposition. Either repair the proof of the pinned "
                "target as stated, or convert it into a witnessed refutation. If neither is possible, "
                "keep the pinned Statement unchanged, retain only the honest partial derivation, and name "
                "the single blocking obligation in the proof. Do not emit a weaker statement just to pass verification."
            )
        return (
            "# Problem\n"
            + self.layout.read_problem()
            + "\n\n# Candidate Proposition File\n"
            + rel
            + "\n\n# Review\n"
            + review_text
            + "\n\n"
            + failed_history
            + no_weakening
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
        verify_history: list[dict[str, Any]] | None = None,
        target_achieved: bool | None = None,
        result_summary_file: Path | None = None,
        difficulty_declaration_file: Path | None = None,
        failure_kind: str | None = None,
        blocking_obligation: str | None = None,
    ) -> WorkerRunResult:
        self._set_phase("done", status=status)
        declaration_file = difficulty_declaration_file
        if declaration_file is None:
            candidate = self.worker_dir / "difficulty_declaration.md"
            declaration_file = candidate if candidate.is_file() else None
        handoff_file: Path | None = None
        handoff: dict[str, Any] | None = None
        if declaration_file is not None:
            handoff_file, handoff = materialize_difficulty_handoff(
                declaration_path=declaration_file,
                worker_id=self.worker_id,
                direction_id=self.direction_id,
                gap_id=self.gap_id,
                method_id=self.method_id,
                execution_status=status,
                failure_kind=failure_kind,
                result_summary_file=result_summary_file,
                review_file=review_file,
                proposition_file=proposition_file,
                verified_file=verified_file,
            )
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
            direction_id=self.direction_id,
            gap_id=self.gap_id,
            solved_problem=solved_problem,
            trace=list(self.trace),
            is_consolidation=self.is_consolidation,
            target_achieved=target_achieved,
            verify_history=verify_history or [],
            worker_hint=self.worker_hint,
            pinned_target=self.pinned_target,
            result_summary_file=result_summary_file,
            difficulty_declaration_file=declaration_file,
            difficulty_handoff_file=handoff_file,
            difficulty_handoff=handoff,
            failure_kind=failure_kind,
            blocking_obligation=blocking_obligation,
            method_id=self.method_id,
            rubric=self.rubric,
        )

    def _event_sink(self, role: str):
        return compose_event_sinks(
            make_worker_event_sink(self.renderer, worker_id=self.worker_id, role=role),
            self._worker_log_sink,
            self.log_session.token_usage_sink("worker") if self.log_session is not None else None,
            # 统一运行日志按具体角色细分（generator / verifier / reviser 等），
            # 便于逐 agent 观察 token 与 CoT。
            self.log_session.run_log_sink(f"worker/{role}") if self.log_session is not None else None,
        )

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

    def _verifier_attempt_config(self, config_name: str) -> AgentConfig:
        config = self.suite.agents[config_name]
        tools = [name for name in config.tools if name not in {"Write", "Edit"}]
        if tools == config.tools:
            return config
        return replace(config, tools=tools)

    def _model_name(self, config: AgentConfig) -> str:
        return config.effective_tier()


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
