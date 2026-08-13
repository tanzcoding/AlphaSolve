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
from .difficulty_dag import DifficultyDagStore, register_orchestrator_difficulty_tools
from .curation_records import append_event as append_curation_event
from .cold_start import ColdStartRuntime
from .policy import SolverPolicy
from .difficulty_portfolio import (
    candidate_handoffs,
    load_recent_candidate_handoffs,
    merge_candidate_handoffs,
)
from .progress_audit import ProgressAuditQueue
from .solution import write_solution
from .client_factory import ClientFactory
from .subagent_service import SubagentService
from .tool_runtime import build_solver_tool_registry, register_orchestrator_worker_tools
from .workspace_access import RoleWorkspaceAccess
from .search import SearchSession
from .research_frontier_state import write_research_frontier_state
from .research_planning import frontier_projection, parse_recommendation, reviewer_prompt

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
        orchestrator_session_id: str | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
        policy: SolverPolicy | None = None,
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
        self.completed_backlog: list[dict[str, Any]] = []
        self._task_output_audit_checkpoints: list[str] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.progress_audit_queue = progress_audit_queue
        self.orchestrator_session_id = orchestrator_session_id
        self.log_session = log_session
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
                "orchestrator_session_id": self.orchestrator_session_id,
                "started_at": time.time(),
                "phase": "spawned",
                "phase_status": "running",
                "phase_updated_at": time.time(),
            }
        future = self.executor.submit(worker.run)
        self.active[future] = worker_id
        payload = {
            "spawned": True,
            "worker_id": worker_id,
            "difficulty_id": difficulty_id,
            "difficulty_statement": difficulty_statement,
            "method_id": method_id,
            **self._pool_status(),
        }
        return payload

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
                }
                self._append_worker_result_log(payload)
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
            payload.update(completion_feedback)
            self._append_worker_result_log(payload)
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
    """orchestrator 调用 subagent 时，由系统决定 research_reviewer 的 read_state。

    - 仅对 ``research_reviewer`` 生效：以 ``epsilon`` 概率「读」state.md（参考历史战略笔记），
      以 ``1-epsilon`` 概率「不读」（保持独立评估、避免被此前的错误路线带偏）。判定式为
      ``rng() < epsilon``；因此 ``epsilon`` 即「读 state.md 的概率」。
    - 其它 subagent 保持调用方显式传入的 read_state（通常为 ``None``，即不注入指令）。
    """
    if agent_type != "research_reviewer":
        return requested
    return rng() < epsilon


class Orchestrator:
    # Backward-compatible default export; live runs use ``SolverPolicy`` instead.
    RESEARCH_REVIEWER_READ_STATE_EPSILON = SolverPolicy().research_reviewer_read_state_epsilon

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
        self._reviewer_call_count = 0
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
        self.difficulty_dag = DifficultyDagStore(
            self.layout.workspace_dir,
            policy=self.policy.difficulty_dag,
        )
        self._progress_audit_queue: ProgressAuditQueue | None = None
        self._planning_subagents: SubagentService | None = None
        self._research_plans: dict[str, dict[str, Any]] = {}
        self._worker_difficulties: dict[str, str] = {}
        # 搜索树观测日志写入器（每 selection cycle 落一次快照）；run() 内按需创建。
        self._search_tree_sink = None

    def _refresh_research_frontier_state(self, manager: WorkerManager | None = None) -> None:
        runtime_provider = getattr(manager, "research_frontier_runtime", None)
        runtime = runtime_provider() if callable(runtime_provider) else None
        policy = getattr(self, "policy", None)
        difficulty_dag_policy = getattr(
            policy,
            "difficulty_dag",
            getattr(getattr(self, "difficulty_dag", None), "policy", None),
        )
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
                orchestrator_session_id=self.session_id,
                log_session=self.log_session,
                stop_event=self.worker_stop_event,
                policy=self.policy,
            )
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
                reviewer_history_path=self.layout.knowledge_dir / "reviewer-history.md",
                reviewer_state_provider=self._reviewer_frontier_projection,
                call_guard=self._guard_subagent_call,
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
            lambda registry: register_orchestrator_difficulty_tools(
                registry,
                snapshot_handler=self._difficulty_frontier_tool,
            ),
            lambda registry: self._register_research_planning_tools(registry, manager),
            lambda registry: self._register_free_exploration_tool(registry, manager),
        )
        if subagents is not None:
            from alphasolve.agent import AgentConfig
            return build_solver_tool_registry(
                access,
                agent_config=AgentConfig(
                    name="_orchestrator_subagent",
                    system_prompt="",
                    tools=["Agent"],
                    tool_parameters={"Agent": {"type": {"enum": ["research_reviewer"]}}},
                ),
                dispatcher=subagents,
                extra_registrars=extra_registrars,
                read_state_resolver=self._reviewer_read_state_resolver,
            )
        return build_solver_tool_registry(access, extra_registrars=extra_registrars)

    def _guard_subagent_call(self, agent_type: str, _depth: int) -> None:
        """Limit reviewer use during the orchestrator phase."""
        if agent_type != "research_reviewer":
            return
        if self._reviewer_call_count:
            raise RuntimeError("research_reviewer may be called at most once per orchestrator run")
        self._reviewer_call_count += 1

    def _reviewer_read_state_resolver(self, agent_type: str, requested: Any) -> bool | None:
        """orchestrator 的 Agent 工具用它决定被调 subagent 的 read_state。

        对 research_reviewer 走 epsilon-greedy（系统决定），并把「是否探索（读 state.md）」
        写进统一运行日志（alphasolve_run.log）；其它 subagent 原样透传调用方请求值。
        """
        epsilon = self.policy.research_reviewer_read_state_epsilon
        decision = _resolve_reviewer_read_state(
            agent_type,
            requested,
            epsilon=epsilon,
        )
        if agent_type == "research_reviewer" and self.log_session is not None:
            mode = "READ state.md (consult prior strategy)" if decision else "SKIP state.md (independent assessment)"
            self.log_session.run_log.note(
                f"research_reviewer read_state decision: {mode} "
                f"[read-probability={epsilon}, read_state={bool(decision)}]"
            )
        return decision

    def _register_free_exploration_tool(self, registry: ToolRegistry, manager: WorkerManager) -> None:
        """注册显式的自由探索工具（不再由代码自动 gate/派发）。

        要不要花一个 worker 槽做无指向的自由探索，完全交给 orchestrator 的 LLM 自己判断
        （见 prompts/orchestrator.md）。这个工具只是把 runtime 已有的「挑一个通用探索 hint、
        可选地从欠探索的知识主题里带一个发散火种」的便利逻辑暴露出来，避免 LLM 手写等价的
        SpawnWorker 调用。
        """
        registry.register(
            name="SpawnFreeExploration",
            description=(
                "Start one worker for open-ended, non-targeted exploration and return immediately "
                "(does not wait). You may use this while canonical leaves exist, but must supply a concrete "
                "reason why an orthogonal route is worth a worker slot. The runtime records that frontier "
                "deviation, the available canonical leaves, and the worker's eventual outcome.\n\n"
                "The worker receives an optional "
                "under-explored verified seed. It must choose a distinct bounded claim or record why no such "
                "claim is available. Returns the same shape as SpawnWorker; if no slot is available it returns "
                "spawned=false."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Concrete reason for departing from the canonical frontier; name the orthogonal hypothesis, evidence, or expected information gain.",
                    },
                },
                "required": ["reason"],
            },
            handler=lambda args: self._spawn_free_exploration_tool(manager, args),
        )

    def _spawn_free_exploration_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
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
                executable_difficulty_ids=deviation.get("executable_difficulty_ids") or [],
            )
            deviations = getattr(self, "_free_exploration_deviations", {})
            deviations[worker_id] = record
            self._free_exploration_deviations = deviations
            payload["frontier_deviation"] = record
            payload["orthogonality_constraints"] = constraints
        self._refresh_research_frontier_state(manager)
        return ToolResult(json.dumps(payload, ensure_ascii=False))

    def _free_exploration_preflight(self) -> dict[str, Any]:
        frontier = self.difficulty_dag.selection_snapshot()
        return {
            "allowed": True,
            "frontier_deviation": {
                "executable_difficulty_ids": [
                    str(item.get("difficulty_id"))
                    for item in frontier.get("executable_difficulties") or []
                    if isinstance(item, dict) and item.get("difficulty_id")
                ],
            },
        }

    def _free_exploration_constraints(self, reason: str, deviation: dict[str, Any]) -> str:
        leaves = ", ".join(f"`{item}`" for item in deviation.get("executable_difficulty_ids") or []) or "none"
        return (
            "Open an independent, bounded research direction. Do not repeat a refuted or split-required obligation. "
            "If this direction yields a new obstacle or a strict child, call RecordDifficulty so the curator can archive it "
            "as an independent graph component or evidence-backed edge at the next checkpoint.\n\n"
            f"Reviewer direction: {reason}\n"
            f"Current executable difficulties: {leaves}"
        )

    def _reviewer_frontier_projection(self) -> dict[str, Any]:
        return frontier_projection(self.difficulty_dag.selection_snapshot())

    def _difficulty_frontier_tool(self, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(json.dumps(self._reviewer_frontier_projection(), ensure_ascii=False))

    def _pending_process_audits(self) -> list[str]:
        queue = self._progress_audit_queue
        if queue is None:
            return []
        status = queue.status_payload()
        return [str(item) for item in status.get("pending_checkpoints") or [] if str(item).strip()]

    def _planning_worker_projection(self, manager: WorkerManager) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for result in list(getattr(manager, "results", [])[-12:]):
            item = {
                "worker_id": result.worker_id,
                "status": result.status,
                "assigned_difficulty_id": result.difficulty_id,
                "method_id": result.method_id,
                "failure_kind": result.failure_kind,
                "summary": _short_summary(result.summary, limit=1000),
                "difficulty_handoff": result.difficulty_handoff,
            }
            projected.append(item)
        return projected

    def _register_research_planning_tools(self, registry: ToolRegistry, manager: WorkerManager) -> None:
        registry.register(
            name="RequestResearchPlan",
            description=(
                "Ask the independent research reviewer to compare completed worker assessments and the current dispatch frontier. "
                "Returns a validated, stored planning recommendation. This tool exposes no raw DAG and does not dispatch work."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: self._request_research_plan_tool(manager, args),
        )
        registry.register(
            name="ExecuteResearchPlan",
            description=(
                "Execute one stored reviewer recommendation after revalidating the current frontier. It may dispatch one current "
                "leaf or launch the recommended independent direction. It rejects stale plans and never writes "
                "canonical DAG structure."
            ),
            parameters={
                "type": "object",
                "properties": {"plan_id": {"type": "string"}},
                "required": ["plan_id"],
            },
            handler=lambda args: self._execute_research_plan_tool(manager, args),
        )

    def _request_research_plan_tool(self, manager: WorkerManager, _args: dict[str, Any]) -> ToolResult:
        service = self._planning_subagents
        if service is None:
            return ToolResult(json.dumps({"error": "research planning is unavailable outside an active orchestrator run"}), is_error=True)
        frontier = self._reviewer_frontier_projection()
        worker_results = self._planning_worker_projection(manager)
        try:
            report = service.call(
                "research_reviewer",
                "Review completed worker assessments and return one validated next-step plan.",
                reviewer_prompt(worker_results=worker_results, frontier=frontier),
            )
        except Exception as exc:
            return ToolResult(json.dumps({"error": f"research reviewer failed: {exc}"}, ensure_ascii=False), is_error=True)
        recommendation = parse_recommendation(report)
        if recommendation is None:
            return ToolResult(json.dumps({"error": "research reviewer did not return a valid Planning Recommendation JSON", "report": report[-4000:]}, ensure_ascii=False), is_error=True)
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        plan = {
            "plan_id": plan_id,
            "frontier_revision": frontier["frontier_revision"],
            "recommendation": recommendation,
            "worker_ids": [item["worker_id"] for item in worker_results],
            "reviewer_report": report,
        }
        self._research_plans[plan_id] = plan
        plan_dir = self.layout.workspace_dir / "curation_records" / "research_plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / f"{plan_id}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return ToolResult(json.dumps({
            "plan_id": plan_id,
            "frontier_revision": plan["frontier_revision"],
            "recommendation": recommendation,
            "reviewer_report": report[-4000:],
        }, ensure_ascii=False))

    def _execute_research_plan_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        plan_id = str(args.get("plan_id") or "").strip()
        plan = self._research_plans.get(plan_id)
        if not plan:
            return ToolResult(json.dumps({"error": "unknown planning recommendation"}), is_error=True)
        frontier = self._reviewer_frontier_projection()
        if frontier["frontier_revision"] != plan["frontier_revision"]:
            return ToolResult(json.dumps({
                "executed": False,
                "reason": "stale_research_plan",
                "expected_frontier_revision": plan["frontier_revision"],
                "current_frontier_revision": frontier["frontier_revision"],
            }, ensure_ascii=False))
        recommendation = plan["recommendation"]
        action = recommendation["action"]
        if action == "DISPATCH_LEAF":
            dispatchable_ids = {
                str(item.get("difficulty_id"))
                for item in frontier["dispatchable"]
                if isinstance(item, dict) and item.get("difficulty_id")
            }
            difficulty_id = recommendation["difficulty_id"]
            if difficulty_id not in dispatchable_ids:
                return ToolResult(json.dumps({"executed": False, "reason": "reviewer_target_not_dispatchable"}, ensure_ascii=False))
            result = self._spawn_difficulty_leaf(manager, {
                "difficulty_id": difficulty_id,
                "method_id": recommendation["method_id"] or "direct_proof",
                "hint": f"Reviewer plan: {recommendation['reason']}",
            })
            payload = json.loads(result.content)
            payload["plan_id"] = plan_id
            return ToolResult(json.dumps(payload, ensure_ascii=False), is_error=result.is_error)
        if action == "DISPATCH_NEW_DIRECTION":
            result = self._spawn_free_exploration_tool(manager, {
                "reason": recommendation["exploration_brief"] or recommendation["reason"],
            })
            payload = json.loads(result.content)
            payload["plan_id"] = plan_id
            return ToolResult(json.dumps(payload, ensure_ascii=False), is_error=result.is_error)
        return ToolResult(json.dumps({"executed": True, "action": action, "plan_id": plan_id}, ensure_ascii=False))

    def _spawn_difficulty_leaf(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        global_attack = bool(args.get("global_attack"))
        requested_id = str(args.get("difficulty_id") or "").strip() or None
        method_id = str(args.get("method_id") or "").strip() or "direct_proof"
        hint = str(args.get("hint") or "").strip() or None
        frontier_refs = [str(item) for item in args.get("frontier_refs") or [] if str(item).strip()]
        frontier_note = str(args.get("frontier_note") or "").strip() or None
        rubric = str(args.get("rubric") or "").strip() or None
        consolidation = bool(args.get("consolidation"))
        pinned_target = str(args.get("pinned_target") or "").strip() or None
        difficulty_statement = None
        preflight: dict[str, Any] = {}
        if consolidation and not global_attack:
            return ToolResult(json.dumps({
                "spawned": False,
                "reason": "global_consolidation_only",
                "message": "consolidation=true is reserved for global_attack. Local leaves may still expose a strict child or missing bridge.",
            }, ensure_ascii=False))
        if global_attack:
            global_directive = self.difficulty_dag.global_attack_preflight()
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
        elif requested_id:
            pending_audits = self._pending_process_audits()
            if pending_audits:
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "process_audit_pending",
                    "pending_checkpoints": pending_audits,
                    "message": "Wait for the current checkpoint audit decision before targeted dispatch.",
                }, ensure_ascii=False))
            try:
                preflight = self.difficulty_dag.dispatch_preflight(
                    difficulty_id=requested_id,
                    method_id=method_id,
                )
            except ValueError as exc:
                return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
            if not preflight.get("allowed"):
                return ToolResult(json.dumps({"spawned": False, **preflight}, ensure_ascii=False))
            difficulty = preflight.get("difficulty") or {}
            difficulty_statement = str(difficulty.get("statement") or "").strip() or None
            mode = str(difficulty.get("dispatch_mode") or "direct")
            if mode == "consolidation":
                method_id = "consolidation"
                pinned_target = difficulty_statement
            assignment_kind = "assigned difficulty"
            assignment = (
                f"# {assignment_kind.title()}\n"
                f"Canonical difficulty: `{requested_id}`\n\n"
                f"Exact obligation:\n{difficulty_statement or '(not recorded)'}\n\n"
                + (
                    "Attempt the local assembly target. If the existing results do not close it, record the exact missing "
                    "bridge, incompatibility, or strict child relation; do not silently retry or broaden scope."
                    if mode == "consolidation"
                    else "Attack this obligation directly. If it weakens, blocks, or exposes a smaller obligation, "
                    "call RecordDifficulty with the exact child relation; do not silently retry or broaden scope."
                )
            )
            hint = assignment + ("\n\n" + hint if hint else "")
        else:
            frontier = self.difficulty_dag.selection_snapshot()
            if frontier.get("executable_difficulties"):
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "difficulty_id_required",
                    "executable_difficulties": frontier["executable_difficulties"],
                    "message": "Choose one curator-owned executable difficulty leaf before spawning targeted work.",
                }, ensure_ascii=False))
            hint = hint or "Explore one bounded route toward problem.md. If blocked or weakened, RecordDifficulty as a root candidate."
        global_on_spawn = (
            (lambda worker: self.difficulty_dag.begin_global_attack(worker_id=worker.worker_id))
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
            on_spawn=global_on_spawn,
        )
        if payload.get("spawned"):
            self._has_dispatched_worker = True
            if preflight.get("dispatch_warnings"):
                payload["dispatch_warnings"] = preflight["dispatch_warnings"]
            if preflight.get("open_child_difficulty_ids"):
                payload["open_child_difficulty_ids"] = preflight["open_child_difficulty_ids"]
        self._refresh_research_frontier_state(manager)
        if payload.get("spawned") and global_attack:
            payload["global_consolidation_directive"] = self.difficulty_dag.global_attack_preflight()
        return ToolResult(json.dumps(payload, ensure_ascii=False), stop_agent=manager.solved_result is not None,
                          stop_answer=_solution_final_answer(manager.solution_path))

    def _spawn_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        return self._spawn_difficulty_leaf(manager, args)

    def _handle_worker_completion(self, result: WorkerRunResult) -> dict[str, Any] | None:
        """Persist completion facts for explicit frontier deviations and global synthesis."""
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
        completed = self.difficulty_dag.complete_global_attack(
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
            "global_consolidation_directive": self.difficulty_dag.global_attack_preflight(),
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
                "## Worker-Reported Integration Gap",
                f"- Event kind: {result.difficulty_handoff.get('event_kind') or 'not stated'}",
                f"- Exact obligation: {result.difficulty_handoff.get('blocking_obligation') or 'not stated'}",
                f"- Verified boundary: {result.difficulty_handoff.get('verified_boundary') or 'not stated'}",
                f"- Remaining delta: {result.difficulty_handoff.get('remaining_delta') or 'not stated'}",
                f"- Refutation witness: {result.difficulty_handoff.get('refutation_witness') or 'not applicable'}",
            ])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except OSError:
            return None
        return path

    def _difficulty_portfolio_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Surface unresolved worker difficulty evidence without re-running the startup reviewer."""
        current = candidate_handoffs(payload.get("completed") or [])
        layout = getattr(self, "layout", None)
        outcomes_path = getattr(layout, "progress_audit_outcomes_path", None)
        persisted = (
            load_recent_candidate_handoffs(outcomes_path)
            if isinstance(outcomes_path, Path)
            else []
        )
        handoffs = merge_candidate_handoffs(persisted, current)
        if not handoffs:
            return None
        return {
            "candidate_worker_ids": [item["worker_id"] for item in handoffs],
            "handoffs": handoffs,
            "review_status": "orchestrator_decision_required",
            "message": (
                "These are worker-level difficulty candidates reconciled only against their own final trail. "
                "Compare their exact obligations and evidence before deciding the next dispatch; reviewer synthesis is "
                "optional and never invoked automatically."
            ),
        }

    def _wait_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        timeout_seconds = args.get("seconds")
        payload = manager.wait(timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None)
        payload["planning_required"] = bool(payload.get("completed"))
        payload["planning_instruction"] = (
            "Use RequestResearchPlan after completed workers; do not infer a target from handoff prose or request the raw DAG."
        )
        if self._search_tree_sink is not None:
            self._search_tree_sink.snapshot(self.search, label="task-output")
        self._refresh_research_frontier_state(manager)
        return ToolResult(json.dumps(payload, ensure_ascii=False), stop_agent=manager.solved_result is not None, stop_answer=_solution_final_answer(manager.solution_path))

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
