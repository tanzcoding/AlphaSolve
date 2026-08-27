from __future__ import annotations

import concurrent.futures
import json
import random
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import AgentRunError, Agent, AgentContextPolicy, AgentEventSink, Workspace
from alphasolve.agent.tools import ToolRegistry, ToolResult
from alphasolve.solver.logging.event_log import compose_event_sinks

from alphasolve.solver.ui.dashboard import make_orchestrator_event_sink
from .worker import Worker, WorkerRunResult
from .project import ProjectLayout
from .research_planning import ReviewerPlanGateway, parse_recommendation, reviewer_prompt, reviewer_strategy_memory
from .curation_records import append_event as append_curation_event
from .cold_start import ColdStartRuntime
from .policy import SolverPolicy
from .difficulty_portfolio import (
    candidate_handoffs,
    load_recent_candidate_handoffs,
    merge_candidate_handoffs,
)
from .progress_audit import ProgressAuditQueue, research_plan_execution_summary
from .solution import write_solution
from .task_audit import TaskAuditor, summarize_task_audits
from .client_factory import ClientFactory
from .subagent_service import SubagentService
from .tool_runtime import build_solver_tool_registry, register_orchestrator_worker_tools
from .workspace_access import RoleWorkspaceAccess
from .search import SearchSession
from .research_frontier_state import write_research_frontier_state

if TYPE_CHECKING:
    from alphasolve.solver.execution import ExecutionGateway
    from alphasolve.solver.logging.log_session import LogSession
    from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer
    from .curator import CuratorQueue


GLOBAL_ATTACK_HINT = (
    "GLOBAL CONSOLIDATION: Attack the original problem directly. "
    "You are free to use ANY mathematical method — do not constrain yourself to "
    "any single framework (e.g., anchor graph, Greene decomposition, or incompatible-cell "
    "counting) that may have dominated prior attempts. Consider alternative approaches: "
    "induction on n, RSK / Young-tableau combinatorics, topological / planar-graph methods, "
    "LP duality, information-theoretic or double-counting arguments, or algebraic methods. "
    "The verified propositions in the workspace are available as building blocks, but you "
    "are encouraged to find a fresh path rather than extending the existing mainstream line. "
    "Either prove the target exactly, or provide an explicit refuting witness."
)


FREE_EXPLORATION_WORKER_HINT = (
    "Explore one genuinely orthogonal route toward `workspace/problem.md`. This is not permission to "
    "repeat a settled fact, a recorded dead end, or the current dominant method under new wording. "
    "Read the injected Free Exploration Constraints before choosing a route. Choose one bounded idea, "
    "state its exact claim, and record a precise difficulty if it does not advance the assigned problem."
)


# 这两条验收标准由运行时定义，因为它们的目标不是 orchestrator 选的：global attack 的
# 目标恒为原题，自由探索的目标恒为"一条有界的正交主张"。其余派发必须由 orchestrator
# 在下发 hint 时自行给出 rubric。
GLOBAL_ATTACK_RUBRIC = (
    "- The Statement resolves `problem.md` exactly as stated, or refutes it with an explicit checkable witness\n"
    "- No weakening: no added hypothesis, reduced bound, special case, or dropped quantifier\n"
    "- Every external step is either proved here or cited from a verified proposition"
)

FREE_EXPLORATION_RUBRIC = (
    "- The Statement is one precise, self-contained mathematical claim\n"
    "- The claim is materially orthogonal to the current dominant method and to recorded dead ends\n"
    "- If the route did not close, a precise obstacle was recorded rather than a vague retry"
)


@dataclass(frozen=True)
class OrchestratorRunResult:
    final_answer: str
    trace: list[dict[str, Any]]
    worker_results: list[WorkerRunResult] = field(default_factory=list)
    solution_path: Path | None = None


class RuntimeInjectionMonitor:
    def __init__(self, *, layout: ProjectLayout, curator_queue: CuratorQueue | None = None) -> None:
        self.layout = layout
        self.curator_queue = curator_queue
        self._known_reference_files = self._scan_reference_files()

    def check(self) -> dict[str, Any] | None:
        hint_updated = self._sync_changed_hint()
        new_reference_files = self._new_reference_files()
        if not hint_updated and not new_reference_files:
            return None

        messages: list[str] = []
        if hint_updated:
            messages.append(
                "hint.md has changed and was copied from the project root into workspace/hint.md; "
                "this may be a human expert injection. Read hint.md before deciding the next action."
            )
        if new_reference_files:
            refs = ", ".join(new_reference_files)
            messages.append(
                "New files appeared under workspace/knowledge/references during this run; "
                f"this may be a human expert injection. Read these files: {refs}."
            )
        return {
            "hint_md_updated": hint_updated,
            "new_reference_files": new_reference_files,
            "message": " ".join(messages),
        }

    def _sync_changed_hint(self) -> bool:
        source = self.layout.hint_path or (self.layout.project_root / "hint.md")
        target = self.layout.workspace_dir / "hint.md"
        if not source.is_file():
            return False
        source_bytes = source.read_bytes()
        target_bytes = target.read_bytes() if target.is_file() else None
        if target_bytes == source_bytes:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True

    def _new_reference_files(self) -> list[str]:
        if (
            self.curator_queue is not None
            and hasattr(self.curator_queue, "has_active_task")
            and self.curator_queue.has_active_task()
        ):
            return []
        current = self._scan_reference_files()
        added = current - self._known_reference_files
        if not added:
            return []

        curator_touched = self._curator_touched_reference_files()
        human_added = sorted(
            rel
            for rel in added
            if rel not in curator_touched
            and rel != "knowledge/references/index.md"
        )
        self._known_reference_files.update(added)
        return human_added

    def _scan_reference_files(self) -> set[str]:
        references_dir = self.layout.knowledge_dir / "references"
        if not references_dir.is_dir():
            return set()
        out: set[str] = set()
        for path in references_dir.rglob("*"):
            if path.is_file():
                out.add(path.resolve().relative_to(self.layout.workspace_dir).as_posix())
        return out

    def _curator_touched_reference_files(self) -> set[str]:
        if self.curator_queue is None or not hasattr(self.curator_queue, "touched_paths"):
            return set()
        references_dir = self.layout.knowledge_dir / "references"
        out: set[str] = set()
        for path in self.curator_queue.touched_paths():
            resolved = Path(path).resolve()
            if resolved.is_file() and (resolved == references_dir or references_dir in resolved.parents):
                out.add(resolved.relative_to(self.layout.workspace_dir).as_posix())
        return out


class WorkerManager:
    # Backward-compatible exports for callers that rely on built-in defaults.
    DEFAULT_WAIT_TIMEOUT_SECONDS = SolverPolicy().worker_wait_timeout_seconds
    VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD = (
        SolverPolicy().verified_propositions_organization_threshold
    )
    FREE_SEED_PROB = SolverPolicy().free_seed_probability

    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int | None = None,
        max_verify_rounds: int | None = None,
        verifier_scaling_factor: int | None = None,
        subagent_max_depth: int | None = None,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        progress_audit_queue: ProgressAuditQueue | None = None,
        task_auditor: TaskAuditor | None = None,
        orchestrator_session_id: str | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
        policy: SolverPolicy | None = None,
        attempt_observer: Any | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        base_policy = policy or SolverPolicy.from_settings(getattr(suite, "settings", None))
        self.policy = base_policy.with_overrides(
            max_workers=max_workers,
            max_verify_rounds=max_verify_rounds,
            verifier_scaling_factor=verifier_scaling_factor,
            subagent_max_depth=subagent_max_depth,
        )
        self.max_workers = self.policy.max_workers
        self.max_verify_rounds = self.policy.max_verify_rounds
        self.verifier_scaling_factor = self.policy.verifier_scaling_factor
        self.subagent_max_depth = self.policy.subagent_max_depth
        self.default_wait_timeout_seconds = self.policy.worker_wait_timeout_seconds
        self.verified_propositions_organization_threshold = (
            self.policy.verified_propositions_organization_threshold
        )
        self.free_seed_probability = self.policy.free_seed_probability
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self.active: dict[concurrent.futures.Future, str] = {}
        self.active_info: dict[str, dict[str, Any]] = {}
        self.active_info_lock = threading.Lock()
        self.results: list[WorkerRunResult] = []
        # 完成 payload 是 reviewer 的实时只读证据层：它保留 task audit 与 route 归因，
        # 而 canonical DAG 只会在异步 curator checkpoint 后更新。
        self.completed_evidence: list[dict[str, Any]] = []
        self.completed_backlog: list[dict[str, Any]] = []
        self._task_output_audit_checkpoints: list[str] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.progress_audit_queue = progress_audit_queue
        # 短程交付验收器（同步）。它与 progress_audit_queue（长程、异步）职责不同：
        # 前者只回答"这次分派被完成了吗"，后者只回答"整体是否在推进原题"。
        self.task_auditor = task_auditor
        self.orchestrator_session_id = orchestrator_session_id
        self.log_session = log_session
        # 只读的 attempt 谱系观测器（SearchSession）。它记录 spawn/result 血缘，
        # 不参与任何调度决策；写入失败绝不能影响 worker 执行。
        self.attempt_observer = attempt_observer
        self.stop_event = stop_event or threading.Event()
        self.solution_path: Path | None = None
        self.solved_result: WorkerRunResult | None = None
        self.completion_handler: Callable[[WorkerRunResult], dict[str, Any] | None] | None = None
        self.injection_monitor = RuntimeInjectionMonitor(layout=self.layout, curator_queue=self.curator_queue)

    def set_completion_handler(self, handler: Callable[[WorkerRunResult], dict[str, Any] | None] | None) -> None:
        self.completion_handler = handler

    def research_frontier_runtime(self) -> dict[str, Any]:
        """Return the current worker pool for the operator-facing frontier snapshot."""
        return self._pool_status()

    def spawn(
        self,
        hint: str | None = None,
        *,
        difficulty_id: str | None = None,
        difficulty_statement: str | None = None,
        method_id: str | None = None,
        frontier_refs: list[str] | None = None,
        frontier_note: str | None = None,
        is_free: bool = False,
        allow_weakening: bool = True,
        pinned_target: str | None = None,
        rubric: str | None = None,
        # 纯归因元数据：不改变 worker 的执行语义，只让这次尝试的"走的是哪条路线"
        # 与"是否图外探索"在结算时可被归档。
        route_label: str | None = None,
        reviewer_step_kind: str | None = None,
        # research plan 归因只服务于后续 process audit / reviewer 复盘；不改变 worker 的数学任务。
        research_plan_id: str | None = None,
        research_track_id: str | None = None,
        track_priority: str | None = None,
        selection_scope: str | None = None,
        tabu_rule_ids: list[str] | None = None,
        on_spawn: Callable[[Worker], None] | None = None,
    ) -> dict[str, Any]:
        self._collect_done()
        if self.solved_result is not None:
            payload = {
                "spawned": False,
                "reason": "problem_already_solved",
                "solution_path": str(self.solution_path) if self.solution_path else None,
                **self._pool_status(),
            }
            return payload
        if len(self.active) >= self.max_workers:
            payload = {
                "spawned": False,
                "reason": "parallelism_limit_reached",
                **self._pool_status(),
            }
            return payload
        worker = Worker(
            layout=self.layout,
            suite=self.suite,
            client_factory=self.client_factory,
            worker_hint=hint,
            difficulty_id=difficulty_id,
            difficulty_statement=difficulty_statement,
            method_id=method_id,
            frontier_refs=frontier_refs,
            frontier_note=frontier_note,
            is_free=is_free,
            allow_weakening=allow_weakening,
            pinned_target=pinned_target,
            rubric=rubric,
            max_verify_rounds=self.max_verify_rounds,
            verifier_scaling_factor=self.verifier_scaling_factor,
            verifier_agents=self.policy.verifier_agents,
            theorem_check_attempts=self.policy.theorem_check_attempts,
            subagent_max_depth=self.subagent_max_depth,
            renderer=self.renderer,
            execution_gateway=self.execution_gateway,
            curator_queue=self.curator_queue,
            stop_event=self.stop_event,
            log_session=self.log_session,
        )
        worker_id = worker.worker_id
        if on_spawn is not None:
            try:
                on_spawn(worker)
            except ValueError as exc:
                return {
                    "spawned": False,
                    "reason": "worker_start_preflight_failed",
                    "message": str(exc),
                    **self._pool_status(),
                }
        if is_free:
            # 自由探索未绑定 canonical difficulty；结束后必须用 RecordDifficulty 形成 root candidate。
            worker.difficulty_id = None
        worker.progress_callback = lambda phase, status, worker_id=worker_id: self._update_worker_progress(
            worker_id,
            phase,
            status,
        )
        if self.renderer is not None:
            self.renderer.register_worker(
                worker_id,
                verified_ctx_size=self._verified_count(),
                remaining_capacity=max(0, self.max_workers - len(self.active) - 1),
            )
        with self.active_info_lock:
            self.active_info[worker_id] = {
                "worker_id": worker_id,
                "worker_dir": str(worker.worker_dir),
                "difficulty_id": difficulty_id,
                "difficulty_statement": difficulty_statement,
                "method_id": method_id,
                "worker_hint": hint,
                "rubric": rubric,
                "pinned_target": pinned_target,
                "route_label": route_label or "",
                "reviewer_step_kind": reviewer_step_kind or "",
                "research_plan_id": research_plan_id or "",
                "research_track_id": research_track_id or "",
                "track_priority": track_priority or "",
                "selection_scope": selection_scope or "",
                "tabu_rule_ids": list(tabu_rule_ids or []),
                "orchestrator_session_id": self.orchestrator_session_id,
                "started_at": time.time(),
                "phase": "spawned",
                "phase_status": "running",
                "phase_updated_at": time.time(),
            }
        future = self.executor.submit(worker.run)
        self.active[future] = worker_id
        self._observe_spawn(
            worker_id=worker_id,
            hint=hint,
            difficulty_id=difficulty_id,
            method_id=method_id,
        )
        payload = {
            "spawned": True,
            "worker_id": worker_id,
            "difficulty_id": difficulty_id,
            "difficulty_statement": difficulty_statement,
            "method_id": method_id,
            "research_plan_id": research_plan_id or "",
            "research_track_id": research_track_id or "",
            "track_priority": track_priority or "",
            "selection_scope": selection_scope or "",
            "tabu_rule_ids": list(tabu_rule_ids or []),
            **self._pool_status(),
        }
        return payload

    def _observe_spawn(
        self,
        *,
        worker_id: str,
        hint: str | None,
        difficulty_id: str | None,
        method_id: str | None,
    ) -> None:
        if self.attempt_observer is None:
            return
        try:
            self.attempt_observer.on_spawn(
                worker_id,
                hint,
                difficulty_id=difficulty_id,
                method_id=method_id,
            )
        except Exception:
            # 观测层是只读附加信号；它绝不能影响真实派发。
            pass

    def _observe_result(self, payload: dict[str, Any]) -> None:
        if self.attempt_observer is None:
            return
        try:
            self.attempt_observer.on_worker_result(payload)
        except Exception:
            # 观测层是只读附加信号；它绝不能隐藏已完成的 worker 结果。
            pass

    def wait(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        self._collect_done()
        if self.completed_backlog:
            completed = list(self.completed_backlog)
            self.completed_backlog.clear()
            return self._with_runtime_updates(self._attach_audit_decisions(self._wait_payload(completed)))
        if not self.active:
            return self._with_runtime_updates(self._attach_audit_decisions({"completed": [], **self._pool_status(), "message": "no active workers"}))
        timeout = self.default_wait_timeout_seconds if timeout_seconds is None else max(1200.0, float(timeout_seconds))
        done, _ = concurrent.futures.wait(
            list(self.active.keys()),
            timeout=timeout,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        if not done:
            return self._with_runtime_updates({
                "completed": [],
                "timed_out": True,
                "timeout_seconds": timeout,
                "message": f"no worker finished within {timeout:g} seconds",
                **self._pool_status(),
            })
        return self._with_runtime_updates(self._attach_audit_decisions(self._wait_payload(self._consume_done(done))))

    def _attach_audit_decisions(self, payload: dict[str, Any]) -> dict[str, Any]:
        checkpoint_ids = list(dict.fromkeys(self._task_output_audit_checkpoints))
        self._task_output_audit_checkpoints.clear()
        if not checkpoint_ids or self.progress_audit_queue is None:
            return payload
        decisions = self.progress_audit_queue.wait_for_decisions(checkpoint_ids)
        payload["process_audit_decisions"] = decisions
        payload["process_audit_decision_required"] = bool(decisions)
        if not decisions:
            payload["process_audit_decision_pending"] = checkpoint_ids
        return payload

    def has_available_worker_slot(self) -> bool:
        self._collect_done()
        return self.solved_result is None and len(self.active) < self.max_workers

    def spawn_free_exploration_if_available(
        self,
        *,
        exploration_constraints: str | None = None,
    ) -> dict[str, Any]:
        if not self.has_available_worker_slot():
            return {
                "spawned": False,
                "reason": "no_available_worker_slot",
                **self._pool_status(),
            }
        seed = self._pick_free_divergence_seed()
        note_parts = [
            "Optional seed from an under-explored verified area; ignore it if it conflicts with the orthogonality constraints."
            if seed else "",
            str(exploration_constraints or "").strip(),
        ]
        note = "\n\n".join(part for part in note_parts if part)
        return self.spawn(
            FREE_EXPLORATION_WORKER_HINT,
            difficulty_id=None,
            method_id="free_exploration",
            frontier_refs=seed or None,
            frontier_note=note or None,
            is_free=True,
            rubric=FREE_EXPLORATION_RUBRIC,
        )

    def _pick_free_divergence_seed(self) -> list[str]:
        """为 free worker 从"欠探索主题目录"随机挑 0–1 个正交 verified prop 当发散火种。

        - 以当前运行策略中的 free seed 概率给 1 个种子，否则返回空（纯白纸）。
        - 主题按 verified_propositions/ 下的顶层目录分组，用反频率权重（prop 越少的主题
          越可能被选中），再在该主题内随机取一个 .md。返回相对 verified 的路径（无扩展名）。
        - 纯代码随机，不经过 research_reviewer，因此天然偏正交而非主流。
        """
        if random.random() > self.free_seed_probability:
            return []
        verified = self.layout.verified_dir
        try:
            md_files = [
                path
                for path in verified.rglob("*.md")
                if path.name not in {"index.md", "state.md"} and path.is_file()
            ]
        except OSError:
            return []
        if not md_files:
            return []
        groups: dict[str, list[Path]] = {}
        for path in md_files:
            rel = path.relative_to(verified)
            topic = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            groups.setdefault(topic, []).append(path)
        topics = list(groups.keys())
        weights = [1.0 / len(groups[topic]) for topic in topics]
        topic = random.choices(topics, weights=weights, k=1)[0]
        chosen = random.choice(groups[topic])
        return [chosen.relative_to(verified).with_suffix("").as_posix()]

    def close(self, *, timeout: float = 5.0, graceful: bool = False) -> None:
        wait_timeout = max(0.0, float(timeout))
        if graceful:
            self.executor.shutdown(wait=False, cancel_futures=False)
            done, not_done = self._wait_for_active(timeout=wait_timeout)
            self._consume_done(done)
            if not_done:
                if self.renderer is not None:
                    remaining = ", ".join(self.active.get(future, "unknown") for future in sorted(not_done, key=id))
                    self.renderer.log(
                        None,
                        "graceful worker shutdown timed out; leaving unfinished workers to unwind in background: "
                        + remaining,
                        module="shutdown",
                        level="WARNING",
                    )
                # 优雅关闭只等有限时间，超时后转入强制收尾，避免某个挂起的调用把整个退出流程永久卡死。
                self.stop_event.set()
                self.executor.shutdown(wait=False, cancel_futures=True)
                self._collect_done()
                return
            self._collect_done()
            self.executor.shutdown(wait=True, cancel_futures=False)
            return

        self.stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        done, _ = self._wait_for_active(timeout=wait_timeout)
        self._consume_done(done)
        self._collect_done()

    def _wait_for_active(
        self,
        *,
        timeout: float,
    ) -> tuple[set[concurrent.futures.Future], set[concurrent.futures.Future]]:
        if not self.active:
            return set(), set()
        return concurrent.futures.wait(
            list(self.active.keys()),
            timeout=timeout,
            return_when=concurrent.futures.ALL_COMPLETED,
        )

    def _collect_done(self) -> None:
        done = [future for future in self.active if future.done()]
        self.completed_backlog.extend(self._consume_done(done))

    def _consume_done(self, done) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        for future in list(done):
            worker_id = self.active.pop(future, None)
            if worker_id is None:
                continue
            with self.active_info_lock:
                finished_info = self.active_info.pop(worker_id, None) or {}
            try:
                result = future.result()
            except Exception as exc:
                if self.renderer is not None:
                    self.renderer.finish_worker(worker_id, status="failed", summary=str(exc))
                payload = {
                    "worker_id": worker_id,
                    "status": "failed",
                    "summary": str(exc),
                "failure_kind": "execution_failed",
                "difficulty_id": finished_info.get("difficulty_id"),
                "method_id": finished_info.get("method_id"),
                "orchestrator_session_id": finished_info.get("orchestrator_session_id")
                or self.orchestrator_session_id,
                # 交付验收需要知道"当初要求了什么"；worker 崩溃时结果对象不存在，
                # 因此这些分派事实只能由调度侧从 active_info 补回。
                "worker_hint": finished_info.get("worker_hint"),
                "rubric": finished_info.get("rubric"),
                "pinned_target": finished_info.get("pinned_target"),
                "route_label": str(finished_info.get("route_label") or ""),
                "reviewer_step_kind": str(finished_info.get("reviewer_step_kind") or ""),
                "research_plan_id": str(finished_info.get("research_plan_id") or ""),
                "research_track_id": str(finished_info.get("research_track_id") or ""),
                "track_priority": str(finished_info.get("track_priority") or ""),
                "selection_scope": str(finished_info.get("selection_scope") or ""),
                "tabu_rule_ids": list(finished_info.get("tabu_rule_ids") or []),
                }
                task_audit = self._run_task_audit(payload)
                if task_audit is not None:
                    payload["task_audit"] = task_audit
                self._append_worker_result_log(payload)
                self._observe_result(payload)
                self._record_completed_evidence(payload)
                completed.append(payload)
                continue
            self.results.append(replace(result, trace=[]))
            completion_feedback: dict[str, Any] = {}
            if self.completion_handler is not None:
                try:
                    completion_feedback = self.completion_handler(result) or {}
                except Exception:
                    # Supplemental orchestration feedback must not hide a completed worker result.
                    completion_feedback = {}
            if result.solved_problem and result.verified_file is not None and self.solved_result is None:
                self.solved_result = result
                self.stop_event.set()
                self.solution_path = write_solution(self.layout, result.verified_file)
            if self.renderer is not None:
                self.renderer.finish_worker(
                    worker_id,
                    status=result.status,
                    summary=_short_summary(result.summary),
                )
                self.renderer.record_commit(
                    accepted=result.status == "verified",
                    status=result.status,
                    solved=result.solved_problem,
                    verified_count=self._verified_count(),
                )
            payload = _worker_result_payload(result)
            # 归因字段由调度侧持有：worker 不知道自己属于哪个 orchestrator session，
            # 而 audit/curator 需要它来判断同一障碍是否跨 session 重复出现。
            payload["orchestrator_session_id"] = (
                finished_info.get("orchestrator_session_id") or self.orchestrator_session_id
            )
            # 同理，路线、图外探索与 research plan/track 都是调度侧归因；它们必须进入
            # immutable outcome，供 process auditor 比较“原计划”与“实际执行结果”。
            for key in ("route_label", "reviewer_step_kind", "research_plan_id", "research_track_id", "track_priority", "selection_scope"):
                value = str(finished_info.get(key) or "").strip()
                if value:
                    payload[key] = value
            if isinstance(finished_info.get("tabu_rule_ids"), list):
                payload["tabu_rule_ids"] = [str(item) for item in finished_info["tabu_rule_ids"] if str(item).strip()]

            payload.update(completion_feedback)
            # 短程任务验收在收割路径内同步执行：它的唯一读者是 orchestrator，且必须在
            # 同一次 TaskOutput 里回答"我刚下发的那个任务被完成了吗"。若异步执行，结论
            # 会晚于 orchestrator 的下一次派发决策而失去意义。
            task_audit = self._run_task_audit(payload)
            if task_audit is not None:
                payload["task_audit"] = task_audit
            self._append_worker_result_log(payload)
            self._observe_result(payload)
            self._record_completed_evidence(payload)
            completed.append(payload)
        if completed and self.progress_audit_queue is not None:
            try:
                self._task_output_audit_checkpoints.extend(
                    self.progress_audit_queue.record_outcomes(completed)
                )
            except Exception:
                # Audit persistence is observational and must not hide worker results.
                pass
        return completed

    def _record_completed_evidence(self, payload: dict[str, Any]) -> None:
        """Retain a bounded realtime evidence window without changing canonical state."""
        evidence = getattr(self, "completed_evidence", None)
        if not isinstance(evidence, list):
            evidence = []
            self.completed_evidence = evidence
        evidence.append(dict(payload))
        del evidence[:-48]

    def _run_task_audit(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Check one finished worker against its acceptance rubric before returning it."""
        if self.task_auditor is None:
            return None
        try:
            return self.task_auditor.audit(payload)
        except Exception:
            # 交付验收是附加信号；它绝不能隐藏一个已完成的 worker 结果。
            return None

    def _wait_payload(self, completed: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "completed": completed,
            **self._pool_status(),
        }
        if self.solved_result is not None:
            payload["solved"] = True
            payload["solution_path"] = str(self.solution_path) if self.solution_path else None
            payload["message"] = "problem solved; orchestration should stop"
        return payload

    def _with_runtime_updates(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates = self.injection_monitor.check()
        if updates is not None:
            payload["human_expert_updates"] = updates
        organization_prompt = self._verified_propositions_organization_prompt()
        if organization_prompt is not None:
            payload["verified_propositions_organization"] = organization_prompt
        if self.progress_audit_queue is not None:
            payload["progress_audit"] = self.progress_audit_queue.status_payload()
        return payload

    def _verified_propositions_organization_prompt(self) -> dict[str, Any] | None:
        overloaded_dirs = self._overloaded_verified_proposition_dirs()
        threshold = self.verified_propositions_organization_threshold
        if not overloaded_dirs:
            return None
        return {
            "threshold": threshold,
            "directories": overloaded_dirs,
            "message": "Organize these verified_propositions directories before spawning more work: "
            + ", ".join(item["path"] for item in overloaded_dirs),
        }

    def _overloaded_verified_proposition_dirs(self) -> list[dict[str, Any]]:
        if not self.layout.verified_dir.is_dir():
            return []
        threshold = self.verified_propositions_organization_threshold
        overloaded: list[dict[str, Any]] = []
        for directory in sorted(
            (path for path in self.layout.verified_dir.rglob("*") if path.is_dir()),
            key=lambda item: item.relative_to(self.layout.verified_dir).as_posix(),
        ):
            direct_markdown_count = sum(1 for path in directory.glob("*.md") if _is_verified_proposition_file(path))
            if direct_markdown_count > threshold:
                overloaded.append({
                    "path": directory.relative_to(self.layout.workspace_dir).as_posix(),
                    "markdown_file_count": direct_markdown_count,
                })
        root_markdown_count = sum(1 for path in self.layout.verified_dir.glob("*.md") if _is_verified_proposition_file(path))
        if root_markdown_count > threshold:
            overloaded.insert(0, {
                "path": "verified_propositions",
                "markdown_file_count": root_markdown_count,
            })
        return overloaded

    def _pool_status(self) -> dict[str, Any]:
        active_workers = [
            self._active_worker_payload(worker_id)
            for worker_id in sorted(self.active.values())
        ]
        return {
            "active_count": len(active_workers),
            "active_worker_ids": [item["worker_id"] for item in active_workers],
            "active_workers": active_workers,
            "max_workers": self.max_workers,
            "available_worker_slots": max(0, self.max_workers - len(active_workers)),
        }

    def _active_worker_payload(self, worker_id: str) -> dict[str, Any]:
        with self.active_info_lock:
            info = dict(self.active_info.get(worker_id, {}))
        elapsed = max(0.0, time.time() - float(info.get("started_at") or time.time()))
        progress = f"running for {elapsed:.0f}s; {_format_worker_phase(str(info.get('phase') or 'starting'))}"
        return {
            "worker_id": worker_id,
            "worker_dir": info.get("worker_dir"),
            "difficulty_id": info.get("difficulty_id"),
            "method_id": info.get("method_id"),
            "progress": progress,
        }

    def _update_worker_progress(self, worker_id: str, phase: str, status: str) -> None:
        with self.active_info_lock:
            info = self.active_info.get(worker_id)
            if info is None:
                return
            info["phase"] = phase
            info["phase_status"] = status
            info["phase_updated_at"] = time.time()

    def _verified_count(self) -> int:
        return verified_count(self.layout.verified_dir)

    def _verified_proposition_total_count(self) -> int:
        return verified_proposition_total_count(self.layout.verified_dir)

    def _append_worker_result_log(self, payload: dict[str, Any]) -> None:
        try:
            self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
            with (self.layout.logs_dir / "worker_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            import sys
            print(
                f"[worker_results] failed to write result for {payload.get('worker_id', '?')}: "
                f"{payload.get('status', '?')}",
                file=sys.stderr,
            )


def _resolve_reviewer_read_state(
    agent_type: str,
    requested: Any,
    *,
    epsilon: float,
    rng: Callable[[], float] = random.random,
) -> bool | None:
    """Deprecated no-op retained only for backward-compatible imports.

    ``read_state`` 曾以 epsilon-greedy 由系统决定 research_reviewer 是否读 state.md。
    现在 reviewer 只消费 orchestrator 显式注入的 canonical 图投影，orchestrator 也不再
    注册 ``Agent`` 工具，因此该策略钩子没有任何生效路径。保留函数签名仅为兼容旧调用方。
    """
    del epsilon, rng
    return requested


class Orchestrator:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        policy: SolverPolicy | None = None,
        max_workers: int | None = None,
        max_verify_rounds: int | None = None,
        verifier_scaling_factor: int | None = None,
        subagent_max_depth: int | None = None,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
        worker_stop_event: threading.Event | None = None,
        session_id: str | None = None,
        cold_start_runtime: ColdStartRuntime | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        base_policy = policy or SolverPolicy.from_settings(getattr(suite, "settings", None))
        self.policy = base_policy.with_overrides(
            max_workers=max_workers,
            max_verify_rounds=max_verify_rounds,
            verifier_scaling_factor=verifier_scaling_factor,
            subagent_max_depth=subagent_max_depth,
        )
        self.max_workers = self.policy.max_workers
        self.max_verify_rounds = self.policy.max_verify_rounds
        self.verifier_scaling_factor = self.policy.verifier_scaling_factor
        self.subagent_max_depth = self.policy.subagent_max_depth
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.log_session = log_session
        self.stop_event = stop_event
        self.worker_stop_event = worker_stop_event or threading.Event()
        self.session_id = session_id or f"orchestrator-{uuid.uuid4().hex[:12]}"
        self._has_dispatched_worker = False
        self.cold_start_runtime = cold_start_runtime or ColdStartRuntime(
            layout=self.layout,
            max_workers=self.max_workers,
            threshold=self.policy.cold_start_verified_proposition_threshold,
            session_id=self.session_id,
            stop_event=self.stop_event,
        )
        self._startup_evidence: list[dict[str, Any]] = []
        # 调度层搜索状态（§2/§3/§5，冻结边界之外的只读附加信号）。它跨整个 run 存活，
        # 只观察 SpawnWorker / TaskOutput 已返回的 payload，不改变 worker 真实执行语义。
        self.search = SearchSession(
            scheduler_state_path=self.layout.scheduler_state_path,
            attempt_graph_path=self.layout.attempt_graph_path,
        )
        self._reviewer_plan_gateway = ReviewerPlanGateway(
            self.layout.workspace_dir,
            policy=self.policy.difficulty_dag,
        )
        self._progress_audit_queue: ProgressAuditQueue | None = None
        self._task_auditor: TaskAuditor | None = None
        self._planning_subagents: SubagentService | None = None
        self._research_plans: dict[str, dict[str, Any]] = {}
        self._research_plan_created = False
        self._active_research_plan: dict[str, Any] | None = None
        self._worker_difficulties: dict[str, str] = {}
        self._local_followup_handoff_ids: set[str] = set()
        # 搜索树观测日志写入器（每 selection cycle 落一次快照）；run() 内按需创建。
        self._search_tree_sink = None

    def _refresh_research_frontier_state(self, manager: WorkerManager | None = None) -> None:
        runtime_provider = getattr(manager, "research_frontier_runtime", None)
        runtime = runtime_provider() if callable(runtime_provider) else None
        policy = getattr(self, "policy", None)
        difficulty_dag_policy = getattr(policy, "difficulty_dag", None)
        try:
            write_research_frontier_state(
                self.layout.workspace_dir,
                runtime=runtime,
                difficulty_dag_policy=difficulty_dag_policy,
            )
        except (OSError, ValueError, KeyError, TypeError):
            # The state file is operator-facing telemetry; it must not interrupt solving.
            pass

    def run(self) -> OrchestratorRunResult:
        append_curation_event(
            self.layout,
            "orchestrator_session_started",
            orchestrator_session_id=self.session_id,
        )
        if self.renderer is not None:
            self.renderer.update_pool(verified_count=self._verified_count())
            self.renderer.update_orchestrator_phase("starting", status="running")
        orchestrator_log_sink = None
        if self.log_session is not None:
            orchestrator_log_sink = self.log_session.create_orchestrator_sink()
            self._search_tree_sink = self.log_session.create_search_sink()
        try:
            progress_audit_queue = ProgressAuditQueue(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                curator_queue=self.curator_queue,
                execution_gateway=self.execution_gateway,
                log_session=self.log_session,
                stop_event=self.stop_event,
                outcomes_per_audit=self.policy.progress_audit_every_n_outcomes,
            )
            progress_audit_queue.start()
            self._progress_audit_queue = progress_audit_queue
            # 短程交付验收：同步运行在 worker 收割路径内，结论随 TaskOutput 一起返回。
            task_auditor = TaskAuditor(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                log_session=self.log_session,
                stop_event=self.stop_event,
            )
            self._task_auditor = task_auditor
            manager = WorkerManager(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                max_workers=self.max_workers,
                max_verify_rounds=self.max_verify_rounds,
                verifier_scaling_factor=self.verifier_scaling_factor,
                subagent_max_depth=self.subagent_max_depth,
                renderer=self.renderer,
                execution_gateway=self.execution_gateway,
                curator_queue=self.curator_queue,
                progress_audit_queue=progress_audit_queue,
                task_auditor=task_auditor,
                orchestrator_session_id=self.session_id,
                log_session=self.log_session,
                stop_event=self.worker_stop_event,
                policy=self.policy,
                attempt_observer=self.search,
            )
            # Bootstrap has no prior portfolio for the reviewer to compare. Preserve the
            # bounded runtime evidence batch, then hand its outcomes to the reviewer for
            # every subsequent route-level reflection or pivot.
            self._startup_evidence = self.cold_start_runtime.prepare(manager)
            self._refresh_research_frontier_state(manager)
            result = None
            error_final_answer = ""
            error_trace: list[dict[str, Any]] = []
            subagents = SubagentService(
                suite=self.suite,
                client_factory=self.client_factory,
                # 直调 reasoning_subagent/numerical_experiment_subagent 时不允许再委派；
                # research_reviewer 是唯一例外——SubagentService._effective_max_depth 会给它
                # 单独放宽一层，用于自行调用 reasoning_subagent 做对抗复核、
                # numerical_experiment_subagent 做数值核验。
                max_depth=0,
                execution_gateway=self.execution_gateway,
                session_prefix="orchestrator",
                allow_research_reviewer=True,
                log_session=self.log_session,
                file_access_factory=lambda: RoleWorkspaceAccess.orchestrator_subagent(
                    Workspace(self.layout.workspace_dir)
                ),
                stop_event=self.stop_event,
                reviewer_state_provider=self._reviewer_frontier_projection,
                # reviewer 跨调用记忆：让它看到自己此前的策略与 next_step，
                # 避免在没有新证据时来回翻转同一条路线。
                reviewer_history_path=self.layout.curation_records_dir / "reviewer_history.md",
            )
            self._planning_subagents = subagents
            try:
                agent = self.build_agent(
                    manager,
                    subagents=subagents,
                    event_sink=compose_event_sinks(
                        make_orchestrator_event_sink(self.renderer),
                        orchestrator_log_sink,
                        self.log_session.token_usage_sink("orchestrator")
                        if self.log_session is not None else None,
                        self.log_session.run_log_sink("orchestrator")
                        if self.log_session is not None else None,
                    ),
                )
                result = agent.run(self._task())
            except AgentRunError as exc:
                error_final_answer = str(exc)
                error_trace = exc.trace
            finally:
                user_requested_stop = (
                    self.stop_event is not None
                    and self.stop_event.is_set()
                    and manager.solved_result is None
                )
                manager.close(graceful=user_requested_stop)
                progress_audit_queue.stop()
                self._progress_audit_queue = None
                self._task_auditor = None
                self._planning_subagents = None
        finally:
            if orchestrator_log_sink is not None:
                orchestrator_log_sink.close()
            if self._search_tree_sink is not None:
                # 收尾再拍一张最终快照，然后关闭。
                self._search_tree_sink.snapshot(self.search, label="final")
                self._search_tree_sink.close()
                self._search_tree_sink = None
            if self.log_session is not None:
                # 收尾刷新并关闭 token 聚合器（此时 workers/subagents 均已结束）。
                # 注意：统一运行日志（alphasolve_run.log）跨 orchestrator restart 存活，
                # 其关闭改由 app.py 的 run 级 finally 负责，这里不再关闭。
                self.log_session.close_token_usage()

        if result is None:
            final_result = OrchestratorRunResult(
                final_answer=error_final_answer,
                trace=error_trace,
                worker_results=list(manager.results),
                solution_path=manager.solution_path,
            )
        else:
            final_result = OrchestratorRunResult(
                final_answer=result.final_answer,
                trace=result.trace,
                worker_results=list(manager.results),
                solution_path=manager.solution_path,
            )
        append_curation_event(
            self.layout,
            "orchestrator_session_finished",
            orchestrator_session_id=self.session_id,
            worker_results=len(final_result.worker_results),
            solved=final_result.solution_path is not None,
            final_answer=(final_result.final_answer or "")[:1000],
        )
        return final_result

    def _model_name(self, config) -> str:
        return config.effective_tier()

    def build_agent(
        self,
        manager: WorkerManager,
        *,
        subagents: SubagentService | None = None,
        event_sink: AgentEventSink | None = None,
        context_policy: AgentContextPolicy | None = None,
    ) -> Agent:
        """按真实 orchestrator 配方装配单个 Agent，供 run() 和外部入口共用。"""
        registry = self._build_registry(manager, subagents=subagents)
        config = self.suite.agents["orchestrator"]
        if self.renderer is not None:
            model_name = self._model_name(config)
            self.renderer.set_orchestrator_model(model_name)
        return Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            event_sink=event_sink,
            stop_event=self.stop_event,
            context_policy=context_policy,
        )

    def _build_registry(self, manager: WorkerManager, *, subagents: SubagentService | None = None) -> ToolRegistry:
        set_completion_handler = getattr(manager, "set_completion_handler", None)
        if callable(set_completion_handler):
            set_completion_handler(self._handle_worker_completion)
        access = RoleWorkspaceAccess.orchestrator(Workspace(self.layout.workspace_dir))
        extra_registrars = (
            lambda registry: register_orchestrator_worker_tools(
                registry,
                spawn_handler=lambda args: self._spawn_tool(manager, args),
                wait_handler=lambda args: self._wait_tool(manager, args),
                default_wait_timeout_seconds=getattr(
                    manager,
                    "default_wait_timeout_seconds",
                    self.policy.worker_wait_timeout_seconds,
                ),
            ),
            lambda registry: self._register_research_planning_tools(registry, manager),
            lambda registry: self._register_free_exploration_tool(registry, manager),
        )
        del subagents
        return build_solver_tool_registry(access, extra_registrars=extra_registrars)

    def _register_free_exploration_tool(self, registry: ToolRegistry, manager: WorkerManager) -> None:
        """保留旧工具名以兼容配置，但图外探索只能由 reviewer 计划触发。"""
        registry.register(
            name="SpawnFreeExploration",
            description=(
                "Start one bounded orthogonal exploration and return immediately. Supply a concrete reason and treat the "
                "result as evidence for the research reviewer; it does not itself establish a technique-level policy or alter the DAG."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Concrete reason for choosing an orthogonal route; name the hypothesis, evidence, or expected information gain.",
                    },
                },
                "required": ["reason"],
            },
            handler=lambda args: self._spawn_free_exploration_tool(manager, args),
        )

    def _available_local_handoffs(
        self,
        manager: WorkerManager,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            records.extend(item for item in payload.get("completed") or [] if isinstance(item, dict))
        for result in list(getattr(manager, "results", [])[-24:]):
            if isinstance(getattr(result, "difficulty_handoff", None), dict):
                records.append({"worker_id": result.worker_id, "difficulty_handoff": result.difficulty_handoff})
        current = candidate_handoffs(records)
        persisted = load_recent_candidate_handoffs(self.layout.progress_audit_outcomes_path)
        handoffs = merge_candidate_handoffs(persisted, current)
        return {
            str(item.get("handoff_id") or ""): item
            for item in handoffs
            if str(item.get("handoff_id") or "")
        }

    def _spawn_free_exploration_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        """Run a compatible ad-hoc exploration and record it as reviewer input evidence."""
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return ToolResult(
                json.dumps({"spawned": False, "reason": "frontier_deviation_reason_required"}, ensure_ascii=False),
                is_error=True,
            )
        preflight = self._free_exploration_preflight()
        if not preflight.get("allowed"):
            return ToolResult(json.dumps({"spawned": False, **preflight}, ensure_ascii=False))
        deviation = dict(preflight.get("frontier_deviation") or {})
        constraints = self._free_exploration_constraints(reason, deviation)
        payload = manager.spawn_free_exploration_if_available(exploration_constraints=constraints)
        if payload.get("spawned"):
            self._has_dispatched_worker = True
            worker_id = str(payload.get("worker_id") or "")
            record = append_curation_event(
                self.layout,
                "frontier_deviation_started",
                worker_id=worker_id,
                reason=reason,
            )
            deviations = getattr(self, "_free_exploration_deviations", {})
            deviations[worker_id] = record
            self._free_exploration_deviations = deviations
            payload["frontier_deviation"] = record
            payload["orthogonality_constraints"] = constraints
        self._refresh_research_frontier_state(manager)
        return ToolResult(json.dumps(payload, ensure_ascii=False))

    def _free_exploration_preflight(self) -> dict[str, Any]:
        return {"allowed": True, "frontier_deviation": {}}

    def _free_exploration_constraints(self, reason: str, deviation: dict[str, Any]) -> str:
        del deviation
        return (
            "Open an independent, bounded research direction. If a concrete obstacle prevents completion, "
            "call RecordDifficulty so the curator can review the worker evidence at the next checkpoint.\n\n"
            f"Reviewer direction: {reason}"
        )

    def _reviewer_frontier_projection(self) -> dict[str, Any]:
        return self._reviewer_plan_gateway.reviewer_frontier()

    def _pending_process_audits(self) -> list[str]:
        queue = self._progress_audit_queue
        if queue is None:
            return []
        status = queue.status_payload()
        return [str(item) for item in status.get("pending_checkpoints") or [] if str(item).strip()]

    def _planning_worker_projection(self, manager: WorkerManager) -> list[dict[str, Any]]:
        """Return recent completed evidence before asynchronous DAG curation catches up.

        The canonical graph remains curator-owned. This projection deliberately exposes
        only settled worker facts already recorded by the orchestrator, so the reviewer
        can distinguish a delivered advance from a verified-but-off-target side result
        and can apply route tabu to the most recent attempts.
        """
        evidence = list(getattr(manager, "completed_evidence", [])[-16:])
        if not evidence:
            # Compatibility fallback for callers that construct a lightweight manager.
            for result in list(getattr(manager, "results", [])[-12:]):
                evidence.append(_worker_result_payload(result))
        projected: list[dict[str, Any]] = []
        for payload in evidence[-16:]:
            if not isinstance(payload, dict):
                continue
            audit = payload.get("task_audit") if isinstance(payload.get("task_audit"), dict) else {}
            handoff = payload.get("difficulty_handoff")
            projected.append({
                "worker_id": str(payload.get("worker_id") or ""),
                "status": str(payload.get("status") or "unknown"),
                "assigned_difficulty_id": payload.get("difficulty_id"),
                "method_id": payload.get("method_id"),
                "route_label": str(payload.get("route_label") or ""),
                "reviewer_step_kind": str(payload.get("reviewer_step_kind") or ""),
                "failure_kind": str(payload.get("failure_kind") or ""),
                "delivery": str(audit.get("delivery") or "unknown"),
                "scope_drift": str(audit.get("scope_drift") or "")[:1200],
                "residual_obligation": str(audit.get("residual_obligation") or "")[:2000],
                "rejection_locus": str(audit.get("rejection_locus") or ""),
                "salvageable_content": str(audit.get("salvageable_content") or "")[:1200],
                "retry_assessment": str(audit.get("retry_assessment") or "")[:1200],
                "pinned_target": str(payload.get("pinned_target") or "")[:2000],
                "verified_file": str(payload.get("verified_file") or ""),
                "summary": _short_summary(str(payload.get("summary") or ""), limit=1000),
                "difficulty_handoff": handoff if isinstance(handoff, dict) else None,
            })
        return projected

    def _register_research_planning_tools(self, registry: ToolRegistry, manager: WorkerManager) -> None:
        registry.register(
            name="RequestResearchPlan",
            description=(
                "Ask the independent research reviewer to compare the graph projection, available worker evidence, audits, and source indexes. "
                "Its plan is advisory: use it when strategy is unclear, a route is stale, or new evidence changes priorities. "
                "It exposes no raw DAG and does not dispatch work."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: self._request_research_plan_tool(manager, args),
        )
        registry.register(
            name="ExecuteResearchPlan",
            description=(
                "Compile one stored reviewer research plan into bounded worker tasks. The reviewer owns research tracks, priorities, "
                "and tabu constraints; you own execution: select tracks that fit the available worker slots, decompose each selected "
                "track into one worker-sized task, and provide one acceptance rubric per task. This tool dispatches only the selected "
                "tracks and never writes canonical DAG structure.\n\n"
                "Use a primary track first. Fill additional available slots with reviewer-provided challenger or supporting tracks only "
                "when they are independent and your bounded tasks respect each track's avoid constraints."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "tasks": {
                        "type": "array",
                        "maxItems": 4,
                        "description": "Bounded executions selected from this plan; each track may appear once. Use an empty list only to acknowledge a reviewer HOLD plan.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "track_id": {"type": "string", "maxLength": 80},
                                "task": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 4000,
                                    "description": "A worker-sized artifact or check that advances this research track.",
                                },
                                "rubric": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 4000,
                                    "description": "3-6 '- ' bullets, each checkable against the proven Statement alone.",
                                },
                            },
                            "required": ["track_id", "task", "rubric"],
                        },
                    },
                },
                "required": ["plan_id", "tasks"],
            },
            handler=lambda args: self._execute_research_plan_tool(manager, args),
        )

    def _request_research_plan_tool(self, manager: WorkerManager, _args: dict[str, Any]) -> ToolResult:
        service = self._planning_subagents
        if service is None:
            return ToolResult(json.dumps({"error": "research planning is unavailable outside an active orchestrator run"}), is_error=True)
        worker_results = self._planning_worker_projection(manager)
        reviewer_frontier = self._reviewer_plan_gateway.reviewer_frontier()
        audit_status = self._progress_audit_queue.status_payload() if self._progress_audit_queue is not None else {}
        # 全局计划执行史是事实投影：它将 plan/track 与 immutable outcomes、verified
        # proposition、task audit、local difficulty 引用串起来，策略取舍仍完全由 reviewer 做。
        try:
            audit_status["research_plan_execution_history"] = research_plan_execution_summary(self.layout.workspace_dir)
            audit_status["reviewer_strategy_memory"] = reviewer_strategy_memory(self.layout.workspace_dir)
        except OSError:
            audit_status["research_plan_execution_history"] = []
            audit_status["reviewer_strategy_memory"] = []
        try:
            report = service.call(
                "research_reviewer",
                "Review the current research portfolio and return a validated multi-track research plan; completed worker evidence may be empty.",
                reviewer_prompt(
                    worker_results=worker_results,
                    frontier=reviewer_frontier,
                    process_audit=audit_status,
                ),
            )
        except Exception as exc:
            return ToolResult(json.dumps({"error": f"research reviewer failed: {exc}"}, ensure_ascii=False), is_error=True)
        recommendation = parse_recommendation(report)
        if recommendation is None:
            # 一次 reviewer 调用可能耗时数百秒并消耗大量 token；解析失败时丢弃全文
            # 等于把那次思考彻底作废。把原始报告落盘，让失败可诊断、内容可人工复用。
            unparsed_path = self._write_unparsed_reviewer_report(report)
            return ToolResult(json.dumps({
                "error": "research reviewer did not return a valid Research Strategy JSON",
                "report_path": unparsed_path,
                "report": report[-4000:],
            }, ensure_ascii=False), is_error=True)
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        plan = {
            "plan_id": plan_id,
            "frontier_revision": reviewer_frontier["frontier_revision"],
            "recommendation": recommendation,
            "worker_ids": [item["worker_id"] for item in worker_results],
            "reviewer_report": report,
        }
        self._research_plans[plan_id] = plan
        self._research_plan_created = True
        self._persist_research_plan(plan)
        return ToolResult(json.dumps({
            "plan_id": plan_id,
            "frontier_revision": plan["frontier_revision"],
            "recommendation": recommendation,
            "reviewer_report": report[-4000:],
        }, ensure_ascii=False))

    def _persist_research_plan(self, plan: dict[str, Any]) -> None:
        """Persist plan creation and execution facts for later independent process audits."""
        layout = getattr(self, "layout", None)
        plan_id = str(plan.get("plan_id") or "").strip()
        if layout is None or not plan_id:
            return
        try:
            directory = layout.workspace_dir / "curation_records" / "research_plans"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{plan_id}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # Plan persistence is audit evidence; dispatch must remain available if the disk is transiently unavailable.
            return

    def _write_unparsed_reviewer_report(self, report: str) -> str:
        """Persist a reviewer report whose strategy JSON could not be parsed."""
        try:
            directory = self.layout.workspace_dir / "curation_records" / "research_plans"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"unparsed-{uuid.uuid4().hex[:12]}.md"
            path.write_text(report, encoding="utf-8")
            return path.relative_to(self.layout.workspace_dir).as_posix()
        except (OSError, ValueError):
            return ""

    def _execute_research_plan_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        """Compile reviewer tracks into orchestrator-owned bounded dispatches.

        The plan identifies mathematical directions only. The supplied task entries are
        the orchestrator's execution decisions: they choose which tracks consume the
        currently free slots and turn each research goal into one auditable artifact.
        """
        plan_id = str(args.get("plan_id") or "").strip()
        plan = self._research_plans.get(plan_id)
        if not plan:
            return ToolResult(json.dumps({"error": "unknown research plan"}), is_error=True)
        if plan.get("execution_status") in {"claimed", "executed", "held"}:
            return ToolResult(json.dumps({
                "error": "research plan has already been executed",
                "plan_id": plan_id,
                "execution_status": plan.get("execution_status"),
                "spawned_worker_ids": plan.get("spawned_worker_ids") or [],
            }, ensure_ascii=True), is_error=True)

        revision = self._reviewer_plan_gateway.validate_frontier_revision(
            frontier_revision=str(plan.get("frontier_revision") or ""),
        )
        if not revision.get("allowed"):
            return ToolResult(json.dumps({
                "plan_id": plan_id,
                "executed": False,
                "reason": revision.get("reason"),
                "message": revision.get("message"),
            }, ensure_ascii=False))

        recommendation = plan["recommendation"]
        research_plan = recommendation["research_plan"]
        tracks = {
            str(track.get("track_id") or ""): track
            for track in research_plan.get("tracks") or []
            if isinstance(track, dict)
        }
        task_specs = args.get("tasks")
        if not isinstance(task_specs, list):
            return ToolResult(json.dumps({"error": "tasks must be an array"}), is_error=True)
        if not tracks:
            if task_specs:
                return ToolResult(json.dumps({"error": "a HOLD plan accepts no worker tasks"}), is_error=True)
            plan["execution_status"] = "held"
            plan["held_at"] = time.time()
            plan["hold_reason"] = research_plan.get("hold_reason") or ""
            plan["selected_track_ids"] = []
            plan["spawned_worker_ids"] = []
            plan["spawned_tracks"] = []
            self._persist_research_plan(plan)
            return ToolResult(json.dumps({
                "plan_id": plan_id,
                "executed": True,
                "reason": "research_plan_hold",
                "hold_reason": plan["hold_reason"],
                "spawned": [],
            }, ensure_ascii=False))
        if not task_specs:
            return ToolResult(json.dumps({"error": "at least one bounded plan task is required unless the plan is HOLD"}), is_error=True)

        previously_spawned = {
            str(item.get("track_id") or "")
            for item in plan.get("spawned_tracks") or []
            if isinstance(item, dict) and item.get("spawned")
        }
        selected: list[tuple[dict[str, str], dict[str, str]]] = []
        seen_track_ids: set[str] = set()
        for raw_task in task_specs[:4]:
            if not isinstance(raw_task, dict):
                return ToolResult(json.dumps({"error": "each plan task must be an object"}), is_error=True)
            track_id = str(raw_task.get("track_id") or "").strip()
            task = str(raw_task.get("task") or "").strip()
            rubric = str(raw_task.get("rubric") or "").strip()
            track = tracks.get(track_id)
            if track is None:
                return ToolResult(json.dumps({"error": "task references a track not present in the research plan", "track_id": track_id}), is_error=True)
            if track_id in seen_track_ids:
                return ToolResult(json.dumps({"error": "a research track may be dispatched once per plan", "track_id": track_id}), is_error=True)
            if track_id in previously_spawned:
                return ToolResult(json.dumps({"error": "research track was already spawned by this plan", "track_id": track_id}), is_error=True)
            if not task or not rubric:
                return ToolResult(json.dumps({"error": "each selected track needs a bounded task and acceptance rubric", "track_id": track_id}), is_error=True)
            seen_track_ids.add(track_id)
            selected.append((track, {"task": task, "rubric": rubric}))

        # Waiting for a free worker is not execution. Keep the plan retryable until a
        # slot exists; otherwise a busy pool would silently consume reviewer advice.
        if not bool(getattr(manager, "has_available_worker_slot", lambda: False)()):
            return ToolResult(json.dumps({
                "plan_id": plan_id,
                "executed": False,
                "reason": "no_available_worker_slot",
                "research_plan": research_plan,
                "spawned": [],
            }, ensure_ascii=False))

        # A reviewer plan is consumed atomically at the policy level: a retry of the tool
        # cannot silently launch the same tracks again. Individual spawn failures are
        # returned as evidence for a subsequent fresh reviewer plan.
        plan["execution_status"] = "claimed"
        plan["claimed_at"] = time.time()
        plan["selected_track_ids"] = list(dict.fromkeys(
            [str(item) for item in plan.get("selected_track_ids") or [] if str(item).strip()]
            + [track["track_id"] for track, _ in selected]
        ))
        self._persist_research_plan(plan)
        spawned: list[dict[str, Any]] = []
        for track, task in selected:
            if not manager.has_available_worker_slot():
                spawned.append({
                    "track_id": track["track_id"],
                    "spawned": False,
                    "reason": "no_available_worker_slot",
                })
                continue
            result = self._spawn_difficulty_leaf(
                manager,
                {
                    "difficulty_id": track["difficulty_id"] or None,
                    "method_id": track["method_id"],
                    "hint": _research_track_worker_hint(research_plan, track, task["task"]),
                    "pinned_target": track["research_goal"],
                    "rubric": task["rubric"],
                    "route_label": track["route_label"],
                    "reviewer_step_kind": track["kind"],
                    "selection_scope": str(track.get("selection_scope") or ("NODE_ROUTE" if track["kind"] == "TARGET_NODE" else "TECHNIQUE_EXPLORATION")),
                    "tabu_rule_ids": [str(item) for item in track.get("tabu_rule_ids") or [] if str(item).strip()],
                    "research_plan_id": plan_id,
                    "research_track_id": track["track_id"],
                    "track_priority": track["priority"],
                    "global_attack": str(track.get("selection_scope") or "") == "GLOBAL_SYNTHESIS",
                },
            )
            try:
                payload = json.loads(result.content)
            except json.JSONDecodeError:
                payload = {"spawned": False, "reason": "invalid_spawn_response"}
            payload["track_id"] = track["track_id"]
            payload["track_priority"] = track["priority"]
            spawned.append(payload)

        worker_ids = [str(item.get("worker_id")) for item in spawned if item.get("spawned") and item.get("worker_id")]
        prior_workers = [str(item) for item in plan.get("spawned_worker_ids") or [] if str(item).strip()]
        prior_tracks = [item for item in plan.get("spawned_tracks") or [] if isinstance(item, dict)]
        all_spawned_tracks = [*prior_tracks, *spawned]
        spawned_track_ids = {
            str(item.get("track_id") or "")
            for item in all_spawned_tracks
            if item.get("spawned") and str(item.get("track_id") or "")
        }
        pending_track_ids = [
            track_id for track_id in plan["selected_track_ids"]
            if track_id not in spawned_track_ids
        ]
        plan["execution_status"] = "partially_executed" if pending_track_ids else "executed"
        plan["executed_at"] = time.time()
        plan["spawned_worker_ids"] = list(dict.fromkeys([*prior_workers, *worker_ids]))
        plan["spawned_tracks"] = all_spawned_tracks
        plan["pending_track_ids"] = list(dict.fromkeys(pending_track_ids))
        self._persist_research_plan(plan)
        self._active_research_plan = plan if worker_ids else None
        return ToolResult(json.dumps({
            "plan_id": plan_id,
            "executed": True,
            "execution_status": plan["execution_status"],
            "pending_track_ids": plan["pending_track_ids"],
            "research_plan": research_plan,
            "spawned": spawned,
            "spawned_worker_ids": worker_ids,
        }, ensure_ascii=False))

    def _spawn_difficulty_leaf(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        global_attack = bool(args.get("global_attack"))
        requested_id = str(args.get("difficulty_id") or "").strip() or None
        method_id = str(args.get("method_id") or "").strip() or "direct_proof"
        hint = str(args.get("hint") or "").strip() or None
        frontier_refs = [str(item) for item in args.get("frontier_refs") or [] if str(item).strip()]
        evidence_refs = [str(item) for item in args.get("evidence_refs") or [] if str(item).strip()]
        followup_handoff_ids = [str(item) for item in args.get("followup_handoff_ids") or [] if str(item).strip()]
        frontier_note = str(args.get("frontier_note") or "").strip() or None
        rubric = str(args.get("rubric") or "").strip() or None
        consolidation = bool(args.get("consolidation"))
        pinned_target = str(args.get("pinned_target") or "").strip() or None
        difficulty_statement = None
        if consolidation and not global_attack:
            return ToolResult(json.dumps({
                "spawned": False,
                "reason": "global_consolidation_only",
                "message": "consolidation=true is reserved for global_attack. Local leaves may still expose a strict child or missing bridge.",
            }, ensure_ascii=False))
        if global_attack:
            global_directive = self._reviewer_plan_gateway.global_attack_preflight()
            if not global_directive.get("ready"):
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": global_directive.get("reason"),
                    "message": global_directive.get("message"),
                    "global_consolidation_directive": global_directive,
                }, ensure_ascii=False))
            requested_id = "global-problem-attack"
            consolidation = True
            if pinned_target is None:
                try:
                    pinned_target = self.layout.read_problem().strip() or None
                except OSError:
                    pinned_target = None
            hint = GLOBAL_ATTACK_HINT
            # global attack 的验收标准是固定的、由运行时定义的：解决原题或给出反驳witness。
            rubric = rubric or GLOBAL_ATTACK_RUBRIC
        else:
            if not rubric:
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "rubric_required",
                    "message": (
                        "State the acceptance rubric together with the hint: 3-6 bullet criteria, each starting with '- ' and "
                        "each checkable against the proven Statement alone. A task auditor uses it to report whether this "
                        "dispatch was delivered, so a dispatch without acceptance criteria cannot be audited."
                    ),
                }, ensure_ascii=False), is_error=True)
            if followup_handoff_ids:
                available_handoffs = self._available_local_handoffs(manager)
                selected_handoffs: list[dict[str, Any]] = []
                for handoff_id in dict.fromkeys(followup_handoff_ids):
                    handoff = available_handoffs.get(handoff_id)
                    if handoff is None:
                        return ToolResult(json.dumps({
                            "spawned": False,
                            "reason": "unknown_local_handoff",
                            "message": "Use followup_handoff_ids returned by TaskOutput.local_difficulties.",
                            "handoff_id": handoff_id,
                        }, ensure_ascii=False))
                    if handoff_id in self._local_followup_handoff_ids:
                        return ToolResult(json.dumps({
                            "spawned": False,
                            "reason": "local_handoff_already_followed_up",
                            "message": "Each local handoff permits one bounded follow-up in this orchestrator run.",
                            "handoff_id": handoff_id,
                        }, ensure_ascii=False))
                    selected_handoffs.append(handoff)
                followup_context = []
                for handoff in selected_handoffs:
                    obstacle = str(handoff.get("obstacle") or "").strip()
                    target = str(handoff.get("assigned_target") or "").strip()
                    if target or obstacle:
                        followup_context.append(
                            f"- Target: {target or '(not recorded)'}\n  Obstacle: {obstacle or '(not recorded)'}"
                        )
                    evidence_refs.extend(
                        str(item) for item in handoff.get("evidence_refs") or [] if str(item).strip()
                    )
                if followup_context:
                    hint = (
                        "# Evidence-backed local follow-up\n"
                        "Use the worker-local evidence below to test, repair, narrow, or refute a concrete obstacle.\n\n"
                        + "\n".join(followup_context)
                        + "\n\n# Assigned bounded task\n"
                        + (hint or "Produce one precise, independently checkable next result.")
                        + "\n\nTreat handoffs as local evidence only. Do not infer canonical graph structure; if blocked, "
                        "RecordDifficulty with the exact remaining obstacle."
                    )
                pinned_target = pinned_target or next(
                    (str(item.get("assigned_target") or "").strip() for item in selected_handoffs if str(item.get("assigned_target") or "").strip()),
                    None,
                )
            hint = hint or "Explore one bounded route toward problem.md. If blocked or weakened, RecordDifficulty with the exact obstacle."
            if evidence_refs:
                evidence_note = "Relevant prior evidence: " + ", ".join(list(dict.fromkeys(evidence_refs))[:16])
                frontier_note = f"{frontier_note}\n{evidence_note}" if frontier_note else evidence_note
            # 引用了 canonical ID 时做一次运行时安全检查：终态（resolved/refuted/superseded）
            # 义务不得被静默重攻，并把该节点的 canonical statement 与 dispatch 警告一并带出，
            # 使 worker 拿到的是它真正被指派的那条义务，而不是只有一个无语义的标签。
            if requested_id:
                preflight = self._reviewer_plan_gateway.dispatch_preflight(
                    difficulty_id=requested_id,
                    method_id=method_id,
                )
                if not preflight.get("allowed"):
                    return ToolResult(json.dumps({
                        "spawned": False,
                        "reason": preflight.get("reason") or "difficulty_not_actionable",
                        "message": preflight.get("message")
                        or "This canonical difficulty is not actionable; choose another bounded target or request reviewer advice.",
                        "difficulty_id": requested_id,
                        "difficulty": preflight.get("difficulty"),
                    }, ensure_ascii=False))
                node = preflight.get("difficulty")
                if isinstance(node, dict):
                    difficulty_statement = str(node.get("statement") or "").strip() or None
                    dispatch_warnings = [
                        str(item) for item in preflight.get("dispatch_warnings") or [] if str(item).strip()
                    ]
                    if dispatch_warnings:
                        frontier_note = "\n".join(
                            part for part in (frontier_note, *dispatch_warnings) if part
                        )
        global_on_spawn = (
            (lambda worker: self._reviewer_plan_gateway.begin_global_attack(worker_id=worker.worker_id))
            if global_attack else None
        )
        payload = manager.spawn(
            hint,
            difficulty_id=requested_id,
            difficulty_statement=difficulty_statement,
            method_id=method_id,
            frontier_refs=frontier_refs or None,
            frontier_note=frontier_note,
            allow_weakening=not global_attack,
            pinned_target=pinned_target,
            rubric=rubric,
            route_label=str(args.get("route_label") or "").strip(),
            reviewer_step_kind=str(args.get("reviewer_step_kind") or "").strip(),
            selection_scope=str(args.get("selection_scope") or "").strip(),
            tabu_rule_ids=[str(item) for item in args.get("tabu_rule_ids") or [] if str(item).strip()],
            research_plan_id=str(args.get("research_plan_id") or "").strip(),
            research_track_id=str(args.get("research_track_id") or "").strip(),
            track_priority=str(args.get("track_priority") or "").strip(),
            on_spawn=global_on_spawn,
        )
        if payload.get("spawned"):
            self._has_dispatched_worker = True
            self._local_followup_handoff_ids.update(followup_handoff_ids)
            if followup_handoff_ids:
                append_curation_event(
                    self.layout,
                    "local_difficulty_followup_started",
                    orchestrator_session_id=self.session_id,
                    source_handoff_ids=list(dict.fromkeys(followup_handoff_ids)),
                    method_id=method_id,
                    brief=(hint or "")[:4000],
                    evidence_refs=list(dict.fromkeys(evidence_refs))[:16],
                    worker_id=payload.get("worker_id"),
                )
        self._refresh_research_frontier_state(manager)
        if payload.get("spawned") and global_attack:
            payload["global_consolidation_directive"] = self._reviewer_plan_gateway.global_attack_preflight()
        solved_result = getattr(manager, "solved_result", None)
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            stop_agent=solved_result is not None,
            stop_answer=_solution_final_answer(getattr(manager, "solution_path", None)) if solved_result is not None else None,
        )

    def _spawn_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        """Keep direct dispatch compatible while preserving its provenance as evidence.

        Direct dispatch is useful at bootstrap and for explicitly requested human-led
        checks. Its outcome is not a reviewer decision: later route reflection, tabu,
        parking, graph departure, and synthesis remain reviewer-owned.
        """
        return self._spawn_difficulty_leaf(manager, args)

    def _handle_worker_completion(self, result: WorkerRunResult) -> dict[str, Any] | None:
        """Persist completion facts and any free-exploration deviation."""
        feedback: dict[str, Any] = {}
        if result.difficulty_id is None and result.method_id == "free_exploration":
            deviation = self._record_free_exploration_completion(result)
            if deviation is not None:
                feedback["frontier_deviation"] = deviation
        if result.difficulty_id != "global-problem-attack":
            return feedback or None
        attempt_id = f"global-{result.worker_id}"
        report_path = self._write_global_attack_report(attempt_id, result)
        report_rel = ""
        if report_path is not None:
            try:
                report_rel = report_path.relative_to(self.layout.workspace_dir).as_posix()
            except ValueError:
                report_rel = str(report_path)
        completed = self._reviewer_plan_gateway.complete_global_attack(
            worker_id=result.worker_id,
            status=result.status,
            target_achieved=result.target_achieved,
            solved_problem=result.solved_problem,
            result_path=report_rel,
        )
        if completed is None:
            return None
        append_curation_event(
            self.layout,
            "global_attack_completed",
            attempt_id=attempt_id,
            status=result.status,
            solved_problem=result.solved_problem,
            target_achieved=result.target_achieved,
            result_path=report_rel,
        )
        if self.curator_queue is not None:
            from .curator import CuratorTask
            self.curator_queue.submit(
                CuratorTask(
                    trace_segment=[],
                    source_label=f"global-attack/{attempt_id}",
                    task_kind="global_attack_review",
                    artifact_path=report_path,
                )
            )
        self._refresh_research_frontier_state()
        return {
            **feedback,
            "global_attack_report": report_rel or None,
            "global_consolidation_directive": self._reviewer_plan_gateway.global_attack_preflight(),
        }

    def _record_free_exploration_completion(self, result: WorkerRunResult) -> dict[str, Any] | None:
        deviations = getattr(self, "_free_exploration_deviations", {})
        started = deviations.pop(result.worker_id, None)
        self._free_exploration_deviations = deviations
        if not isinstance(started, dict):
            return None

        def relative(path: Path | None) -> str | None:
            if path is None:
                return None
            try:
                return path.relative_to(self.layout.workspace_dir).as_posix()
            except ValueError:
                return str(path)

        event = append_curation_event(
            self.layout,
            "frontier_deviation_completed",
            worker_id=result.worker_id,
            reason=started.get("reason"),
            status=result.status,
            summary=result.summary,
            failure_kind=result.failure_kind,
            solved_problem=bool(result.solved_problem),
            verified_file=relative(result.verified_file),
            difficulty_handoff_file=relative(result.difficulty_handoff_file),
        )
        self._refresh_research_frontier_state()
        return event

    def _write_global_attack_report(self, attempt_id: str, result: WorkerRunResult) -> Path | None:
        """Publish bounded synthesis evidence outside the private unverified worker tree."""
        path = self.layout.global_attack_results_dir / f"{attempt_id}.md"
        review = _read_worker_artifact(result.review_file, limit=8000)
        theorem_check = _read_worker_artifact(result.theorem_check_file, limit=8000)
        lines = [
            f"# Global Consolidation Report: {attempt_id}",
            "",
            "This runtime report records a full-problem synthesis attempt for curator review. It is evidence about strategy and integration gaps, not a verified proof unless `solved_problem` is true.",
            "",
            "## Attempt Outcome",
            f"- Status: `{result.status}`",
            f"- Solves original problem: `{bool(result.solved_problem)}`",
            f"- Pinned target achieved: `{result.target_achieved}`",
            f"- Failure kind: `{result.failure_kind or '-'}`",
            f"- Blocking obligation: {result.blocking_obligation or 'not stated'}",
            "",
            "## Full Target",
            result.pinned_target or self.layout.read_problem(),
            "",
            "## Runtime Outcome",
            result.summary or "No runtime outcome was recorded.",
            "",
            "## Verified Evidence Available for This Synthesis",
            *(_verified_proposition_paths(self.layout) or ["- None recorded."]),
            "",
            "## Verification Trail",
        ]
        if result.verify_history:
            for item in result.verify_history:
                lines.append(
                    f"- Round {item.get('round', '?')}: `{item.get('verdict', 'unknown')}` — "
                    f"{str(item.get('review_excerpt') or '')[:1200]}"
                )
        else:
            lines.append("- No verifier workflow completed.")
        if review:
            lines.extend(["", "## Final Review", review])
        if theorem_check:
            lines.extend(["", "## Theorem Check", theorem_check])
        if result.difficulty_handoff:
            lines.extend([
                "",
                "## Worker-Reported Obstacle",
                f"- Obstacle: {result.difficulty_handoff.get('obstacle') or 'not stated'}",
            ])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except OSError:
            return None
        return path

    def _wait_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        timeout_seconds = args.get("seconds")
        payload = manager.wait(timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None)
        local_handoffs = self._available_local_handoffs(manager, payload)
        if local_handoffs:
            payload["local_difficulties"] = list(local_handoffs.values())
        # 短程交付验收（每个完成的 worker 一份）已在收割时同步完成，这里只做汇总。
        task_audit_summary = summarize_task_audits([
            item.get("task_audit")
            for item in payload.get("completed") or []
            if isinstance(item, dict) and isinstance(item.get("task_audit"), dict)
        ])
        if task_audit_summary is not None:
            payload["task_audit_summary"] = task_audit_summary
        audit_decisions = [
            item for item in payload.get("process_audit_decisions") or [] if isinstance(item, dict)
        ]
        if any(str(item.get("verdict") or "").upper() in {"STALLED", "MISALIGNED"} for item in audit_decisions):
            payload["strategic_reassessment_recommended"] = True
        payload["planning_instruction"] = (
            "Task audits and periodic process audits are evidence only. Read delivery, residual_obligation, rejection_locus, "
            "salvageable_content, retry_assessment, terminal_gap, and local difficulties through RequestResearchPlan. The research "
            "reviewer alone decides whether these facts justify a repair, pivot, graph-level exploration, technique-level departure, "
            "parking, or synthesis; execute only its approved tracks."
        )
        if self._search_tree_sink is not None:
            # 推进 selection cycle 并落一次 attempt 谱系快照（纯观测，不参与决策）。
            try:
                self.search.advise()
            except Exception:
                pass
            self._search_tree_sink.snapshot(self.search, label="task-output")
        self._refresh_research_frontier_state(manager)
        return ToolResult(
            json.dumps(_decision_first_payload(payload), ensure_ascii=False),
            stop_agent=manager.solved_result is not None,
            stop_answer=_solution_final_answer(manager.solution_path) if manager.solved_result is not None else None,
        )

    def _task(self) -> str:
        hint = self.layout.read_hint()
        parts = [
            "Use workers to solve the problem stated in `problem.md`.",
            f"Maximum concurrent workers: {self.max_workers}",
        ]
        startup_context = self.cold_start_runtime.context_for_orchestrator()
        if startup_context:
            parts.append(startup_context)
        if hint:
            parts.append("A human expert hint is available in `hint.md`; read it before deciding the next action.")
        return "\n\n".join(parts)

    def _verified_count(self) -> int:
        return verified_count(self.layout.verified_dir)


def verified_count(verified_dir: Path) -> int:
    return verified_proposition_total_count(verified_dir)


def verified_proposition_total_count(verified_dir: Path) -> int:
    if not verified_dir.exists():
        return 0
    return sum(1 for path in verified_dir.rglob("*.md") if _is_verified_proposition_file(path))


def _is_verified_proposition_file(path: Path) -> bool:
    return path.is_file() and path.suffix == ".md" and path.name not in {"index.md", "state.md"}


def _worker_result_payload(result: WorkerRunResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_id": result.worker_id,
        "status": result.status,
        "summary": result.summary,
        "worker_dir": str(result.worker_dir),
        "difficulty_id": result.difficulty_id,
        "method_id": result.method_id,
        "failure_kind": result.failure_kind,
        "blocking_obligation": result.blocking_obligation,
        "solved_problem": result.solved_problem,
    }
    if result.proposition_file:
        payload["proposition_file"] = str(result.proposition_file)
    if result.verified_file:
        payload["verified_file"] = str(result.verified_file)
    if result.review_file:
        payload["review_file"] = str(result.review_file)
    if result.theorem_check_file:
        payload["theorem_check_file"] = str(result.theorem_check_file)
    # consolidation worker 的结构化反馈
    if result.is_consolidation:
        payload["is_consolidation"] = True
        payload["target_achieved"] = result.target_achieved
    # rejected worker 的失败反馈：保留 verifier 轨迹供 process audit 直接核对。
    # （对 consolidation worker 也适用，上面已设 target_achieved）
    if result.status == "rejected":
        if result.verify_history:
            payload["verify_history"] = result.verify_history
        payload["unverified_dir"] = str(result.worker_dir)
    # generator/reviser 产出的最小障碍声明，供 audit 与 curator 按证据核对。
    if result.difficulty_declaration_file:
        payload["difficulty_declaration_file"] = str(result.difficulty_declaration_file)
    # 这是根据最终 revision/review 轨迹打包的候选困难卡片；仍需 reviewer/curator 跨尝试确认。
    if result.difficulty_handoff_file:
        payload["difficulty_handoff_file"] = str(result.difficulty_handoff_file)
    if result.difficulty_handoff:
        payload["difficulty_handoff"] = result.difficulty_handoff
    # 回传 hint 和 pinned_target，便于 orchestrator 对比"要求什么" vs "拿到了什么"
    if result.pinned_target:
        payload["pinned_target"] = result.pinned_target
    if result.worker_hint:
        payload["worker_hint"] = result.worker_hint[:2000]
    if result.rubric:
        payload["rubric"] = result.rubric
    return payload


def _verified_proposition_paths(layout: ProjectLayout) -> list[str]:
    try:
        paths = sorted(
            path for path in layout.verified_dir.rglob("*.md")
            if path.is_file() and path.name not in {"index.md", "state.md"}
        )
    except OSError:
        return []
    return [f"- `{path.relative_to(layout.workspace_dir).as_posix()}`" for path in paths]


def _read_worker_artifact(path: Path | None, *, limit: int) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")[:limit].strip()
    except OSError:
        return ""


def _short_summary(summary: str, *, limit: int = 180) -> str:
    clean = " ".join((summary or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


# TaskOutput 的决策类字段：必须排在 completed 之前。
_DECISION_FIRST_KEYS = (
    "solved",
    "message",
    "solution_path",
    "planning_instruction",
    "task_audit_summary",
    "strategic_reassessment_recommended",
    "process_audit_decisions",
    "timed_out",
    "active_count",
    "available_worker_slots",
    "max_workers",
)


def _decision_first_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reorder one TaskOutput payload so the decision surface precedes the bulk evidence.

    ``completed`` carries full worker reports and routinely runs to tens of thousands of
    characters. Serialized first, it pushes the audit summary and planning instruction to
    the very end of the JSON, which is the easiest place for a reader to lose them. The
    content is unchanged; only key order is, so nothing downstream that looks a field up
    by name is affected.
    """
    ordered: dict[str, Any] = {}
    for key in _DECISION_FIRST_KEYS:
        if key in payload:
            ordered[key] = payload[key]
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _research_track_worker_hint(
    research_plan: dict[str, Any],
    track: dict[str, Any],
    bounded_task: str,
) -> str:
    """Render a reviewer-owned research track plus an orchestrator-owned task."""
    tabu_by_id = {
        str(rule.get("tabu_id") or ""): rule
        for rule in research_plan.get("tabu_rules") or []
        if isinstance(rule, dict)
    }
    applicable_tabu = [
        tabu_by_id[rule_id]
        for rule_id in track.get("tabu_rule_ids") or []
        if rule_id in tabu_by_id
    ]
    parts = [
        "# Reviewer Research Plan",
        f"Objective: {research_plan.get('objective') or ''}",
        f"Strategy: {research_plan.get('strategy') or ''}",
        "",
        "## Research track",
        f"Track: {track.get('track_id') or ''} ({track.get('priority') or ''})",
        f"Selection scope: {track.get('selection_scope') or ''}",
        f"Research goal: {track.get('research_goal') or ''}",
        f"Rationale: {track.get('rationale') or ''}",
    ]
    if track.get("terminal_obligation"):
        parts.append(f"Terminal obligation: {track['terminal_obligation']}")
    if track.get("reopen_condition"):
        parts.append(f"Reopen condition: {track['reopen_condition']}")
    if track.get("avoid"):
        parts.append(f"Avoid: {track['avoid']}")
    if applicable_tabu:
        parts.append("Reviewer tabu context:")
        for rule in applicable_tabu:
            parts.append(
                f"- [{rule.get('level')}] {rule.get('route_label')}: {rule.get('mechanism')} "
                f"Reopen only if: {rule.get('reopen_condition') or 'reviewer supplies new evidence.'}"
            )
    parts.extend(["", "## Bounded worker task", bounded_task])
    return "\n".join(parts)


def _format_worker_phase(phase: str) -> str:
    clean = " ".join((phase or "starting").split())
    if clean == "generator":
        return "current agent: generator"
    if clean.startswith("verifier_attempt"):
        match = re.search(r"w(\d+)\.(\d+)", clean)
        if match:
            return f"current agent: verifier, round {match.group(1)}, attempt {match.group(2)}"
        return "current agent: verifier"
    if clean.startswith("review_verdict_judge"):
        match = re.search(r"w(\d+)\.(\d+)", clean)
        if match:
            return f"current agent: verifier verdict judge, round {match.group(1)}, attempt {match.group(2)}"
        return "current agent: verifier verdict judge"
    if clean.startswith("reviser"):
        match = re.search(r"w(\d+)", clean)
        if match:
            return f"current agent: reviser, round {match.group(1)}"
        return "current agent: reviser"
    if clean == "theorem_checker":
        return "current agent: theorem_checker"
    if clean == "done":
        return "current agent: done"
    return f"current agent: {clean}"


def _solution_final_answer(solution_path: Path | None) -> str:
    if solution_path is None:
        return "Problem solved."
    return f"Problem solved. Solution written to {solution_path}."
