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
from .blocker_registry import PersistentBlockerRegistry
from .curation_records import append_event as append_curation_event
from .difficulty_portfolio import (
    build_reviewer_prompt as build_difficulty_reviewer_prompt,
    candidate_handoffs,
    load_recent_candidate_handoffs,
    merge_candidate_handoffs,
    review_batch_key,
)
from .progress_audit import ProgressAuditQueue
from .solution import write_solution
from .client_factory import ClientFactory
from .subagent_service import SubagentService
from .tool_runtime import (
    build_solver_tool_registry,
    register_orchestrator_research_tools,
    register_orchestrator_worker_tools,
)
from .workspace_access import RoleWorkspaceAccess
from .research_state import ResearchStateStore
from .search import SearchSession

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
    DEFAULT_WAIT_TIMEOUT_SECONDS = 3600.0
    VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD = 20
    # free 探索槽的"发散种子"配额：以此概率给 free worker 注入 1 个来自欠探索主题的正交
    # 火种，否则纯白纸起步（0 种子）。种子只是可选启发，且 free worker 被禁用 reviewer/主流。
    FREE_SEED_PROB = 0.6

    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int,
        max_verify_rounds: int,
        verifier_scaling_factor: int,
        subagent_max_depth: int,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        progress_audit_queue: ProgressAuditQueue | None = None,
        orchestrator_session_id: str | None = None,
        context_generation_provider: Callable[[], int] | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.max_workers = max(1, int(max_workers))
        self.max_verify_rounds = max_verify_rounds
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = subagent_max_depth
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self.active: dict[concurrent.futures.Future, str] = {}
        self.active_info: dict[str, dict[str, Any]] = {}
        self.active_info_lock = threading.Lock()
        self.results: list[WorkerRunResult] = []
        self.completed_backlog: list[dict[str, Any]] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.progress_audit_queue = progress_audit_queue
        self.orchestrator_session_id = orchestrator_session_id
        self.context_generation_provider = context_generation_provider
        self.log_session = log_session
        self.stop_event = stop_event or threading.Event()
        self.solution_path: Path | None = None
        self.solved_result: WorkerRunResult | None = None
        self.injection_monitor = RuntimeInjectionMonitor(layout=self.layout, curator_queue=self.curator_queue)

    def spawn(
        self,
        hint: str | None = None,
        *,
        route_id: str | None = None,
        direction_id: str | None = None,
        gap_id: str | None = None,
        method_id: str | None = None,
        frontier_refs: list[str] | None = None,
        frontier_note: str | None = None,
        is_free: bool = False,
        allow_weakening: bool = True,
        pinned_target: str | None = None,
        rubric: str | None = None,
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
            route_id=route_id,
            direction_id=direction_id,
            gap_id=gap_id,
            method_id=method_id,
            frontier_refs=frontier_refs,
            frontier_note=frontier_note,
            is_free=is_free,
            allow_weakening=allow_weakening,
            pinned_target=pinned_target,
            rubric=rubric,
            max_verify_rounds=self.max_verify_rounds,
            verifier_scaling_factor=self.verifier_scaling_factor,
            subagent_max_depth=self.subagent_max_depth,
            renderer=self.renderer,
            execution_gateway=self.execution_gateway,
            curator_queue=self.curator_queue,
            stop_event=self.stop_event,
            log_session=self.log_session,
        )
        worker_id = worker.worker_id
        if is_free:
            # 每次自由探索都是尚未晋升的独立候选方向，不能永久混进一个共享 arm。
            direction_id = f"free-exploration-{worker_id}"
            gap_id = f"open-exploration-{worker_id}"
            worker.direction_id = direction_id
            worker.gap_id = gap_id
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
                "route_id": route_id,
                "direction_id": direction_id,
                "gap_id": gap_id,
                "method_id": method_id,
                "orchestrator_session_id": self.orchestrator_session_id,
                "context_generation": self._context_generation(),
                "started_at": time.time(),
                "phase": "spawned",
                "phase_status": "running",
                "phase_updated_at": time.time(),
            }
        future = self.executor.submit(worker.run)
        self.active[future] = worker_id
        if self.progress_audit_queue is not None:
            future.add_done_callback(
                lambda done_future, worker_id=worker_id: self._record_completed_worker_for_progress_audit(
                    done_future,
                    worker_id,
                )
            )
        payload = {
            "spawned": True,
            "worker_id": worker_id,
            "route_id": route_id,
            "direction_id": direction_id,
            "gap_id": gap_id,
            "method_id": method_id,
            **self._pool_status(),
        }
        return payload

    def wait(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        self._collect_done()
        if self.completed_backlog:
            completed = list(self.completed_backlog)
            self.completed_backlog.clear()
            return self._with_runtime_updates(self._wait_payload(completed))
        if not self.active:
            return self._with_runtime_updates({"completed": [], **self._pool_status(), "message": "no active workers"})
        timeout = self.DEFAULT_WAIT_TIMEOUT_SECONDS if timeout_seconds is None else max(1200.0, float(timeout_seconds))
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
        return self._with_runtime_updates(self._wait_payload(self._consume_done(done)))

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
            direction_id="free-exploration",
            gap_id="open-exploration",
            method_id="free_exploration",
            frontier_refs=seed or None,
            frontier_note=note or None,
            is_free=True,
        )

    def _pick_free_divergence_seed(self) -> list[str]:
        """为 free worker 从"欠探索主题目录"随机挑 0–1 个正交 verified prop 当发散火种。

        - 以 ``FREE_SEED_PROB`` 概率给 1 个种子，否则返回空（纯白纸）。
        - 主题按 verified_propositions/ 下的顶层目录分组，用反频率权重（prop 越少的主题
          越可能被选中），再在该主题内随机取一个 .md。返回相对 verified 的路径（无扩展名）。
        - 纯代码随机，不经过 research_reviewer，因此天然偏正交而非主流。
        """
        if random.random() > self.FREE_SEED_PROB:
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
                "route_id": finished_info.get("route_id"),
                "direction_id": finished_info.get("direction_id"),
                    "gap_id": finished_info.get("gap_id"),
                    "method_id": finished_info.get("method_id"),
                }
                self._append_worker_result_log(payload)
                completed.append(payload)
                continue
            self.results.append(replace(result, trace=[]))
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
            self._append_worker_result_log(payload)
            completed.append(payload)
        return completed

    def _record_completed_worker_for_progress_audit(
        self,
        future: concurrent.futures.Future,
        worker_id: str,
    ) -> None:
        """Publish a terminal outcome without mutating the normal completion lifecycle."""
        if self.progress_audit_queue is None:
            return
        with self.active_info_lock:
            info = dict(self.active_info.get(worker_id, {}))
        try:
            result = future.result()
        except Exception as exc:
            payload = {
                "worker_id": worker_id,
                "status": "failed",
                "summary": str(exc),
                "failure_kind": "execution_failed",
                "route_id": info.get("route_id"),
                "direction_id": info.get("direction_id"),
                "gap_id": info.get("gap_id"),
                "method_id": info.get("method_id"),
                "worker_dir": info.get("worker_dir"),
                "orchestrator_session_id": info.get("orchestrator_session_id"),
                "context_generation": info.get("context_generation"),
            }
        else:
            payload = {
                **_worker_result_payload(result),
                "orchestrator_session_id": info.get("orchestrator_session_id"),
                "context_generation": info.get("context_generation"),
            }
        try:
            self.progress_audit_queue.record_outcome(payload)
        except Exception:
            # Auditing is observational. It must never turn a completed worker into a failed run.
            pass

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
        threshold = self.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD
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
        threshold = self.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD
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
            "route_id": info.get("route_id"),
            "direction_id": info.get("direction_id"),
            "gap_id": info.get("gap_id"),
            "method_id": info.get("method_id"),
            "progress": progress,
        }

    def _context_generation(self) -> int:
        if self.context_generation_provider is None:
            return 0
        try:
            return max(0, int(self.context_generation_provider()))
        except Exception:
            return 0

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
    # research_reviewer 是否读 state.md，由系统以此概率随机决定：
    # 该值 = 「读 state.md 的概率」。当前 0.0 —— 强制 100% 不读 state.md（完全独立探索、
    # 不参考任何历史战略笔记），用于逼系统跳出旧框、尝试全新方向。
    # （提高该值可让 reviewer 以对应概率参考历史策略；1.0 则每次都读。）
    RESEARCH_REVIEWER_READ_STATE_EPSILON = 0.0

    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int,
        max_verify_rounds: int,
        verifier_scaling_factor: int,
        subagent_max_depth: int,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
        worker_stop_event: threading.Event | None = None,
        session_id: str | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.max_workers = max_workers
        self.max_verify_rounds = max_verify_rounds
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = subagent_max_depth
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.log_session = log_session
        self.stop_event = stop_event
        self.worker_stop_event = worker_stop_event or threading.Event()
        self.session_id = session_id or f"orchestrator-{uuid.uuid4().hex[:12]}"
        self.context_generation = 0
        # 调度层搜索状态（§2/§3/§5，冻结边界之外的只读附加信号）。它跨整个 run 存活，
        # 只观察 SpawnWorker / TaskOutput 已返回的 payload，不改变 worker 真实执行语义。
        self.search = SearchSession(
            scheduler_state_path=self.layout.scheduler_state_path,
            attempt_graph_path=self.layout.attempt_graph_path,
        )
        self.research_state = ResearchStateStore(self.layout.verified_dir)
        self.research_state.ensure_initialized()
        self.blocker_registry = PersistentBlockerRegistry(self.layout.workspace_dir)
        self.search.sync_research_state(self.research_state.load())
        # 搜索树观测日志写入器（每 selection cycle 落一次快照）；run() 内按需创建。
        self._search_tree_sink = None
        self._pending_context_reset: dict[str, Any] | str | None = None
        self._reset_reviewer_service: SubagentService | None = None
        self._handled_process_audit_resets: set[str] = set()

    def run(self) -> OrchestratorRunResult:
        append_curation_event(
            self.layout,
            "orchestrator_session_started",
            orchestrator_session_id=self.session_id,
            context_generation=self.context_generation,
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
                outcomes_per_audit=int(
                    getattr(self.suite, "settings", {}).get("progress_audit_every_n_outcomes", 5)
                ),
                no_progress_outcomes_before_reset=int(
                    getattr(self.suite, "settings", {}).get(
                        "progress_audit_no_progress_outcomes_before_reset",
                        10,
                    )
                ),
            )
            progress_audit_queue.start()
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
                context_generation_provider=lambda: self.context_generation,
                log_session=self.log_session,
                stop_event=self.worker_stop_event,
            )
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
                reviewer_state_provider=self.research_state.reviewer_snapshot,
            )
            self._reset_reviewer_service = subagents
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
            context_generation=self.context_generation,
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
            context_reset_provider=self._consume_process_audit_context_reset,
        )

    def _build_registry(self, manager: WorkerManager, *, subagents: SubagentService | None = None) -> ToolRegistry:
        access = RoleWorkspaceAccess.orchestrator(Workspace(self.layout.workspace_dir))
        extra_registrars = (
            lambda registry: register_orchestrator_worker_tools(
                registry,
                spawn_handler=lambda args: self._spawn_tool(manager, args),
                wait_handler=lambda args: self._wait_tool(manager, args),
                default_wait_timeout_seconds=WorkerManager.DEFAULT_WAIT_TIMEOUT_SECONDS,
            ),
            lambda registry: register_orchestrator_research_tools(
                registry,
                record_impact_handler=self._record_research_impact_tool,
                record_dispatch_constraint_handler=self._record_dispatch_constraint_tool,
                sync_state_handler=self._sync_research_state_tool,
                register_route_handler=self._register_research_route_tool,
                assess_route_handler=self._assess_research_route_tool,
            ),
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
                    tool_parameters={"Agent": {"type": {"enum": [
                        "reasoning_subagent", "research_reviewer", "numerical_experiment_subagent",
                    ]}}},
                ),
                dispatcher=subagents,
                extra_registrars=extra_registrars,
                read_state_resolver=self._reviewer_read_state_resolver,
            )
        return build_solver_tool_registry(access, extra_registrars=extra_registrars)

    def _reviewer_read_state_resolver(self, agent_type: str, requested: Any) -> bool | None:
        """orchestrator 的 Agent 工具用它决定被调 subagent 的 read_state。

        对 research_reviewer 走 epsilon-greedy（系统决定），并把「是否探索（读 state.md）」
        写进统一运行日志（alphasolve_run.log）；其它 subagent 原样透传调用方请求值。
        """
        decision = _resolve_reviewer_read_state(
            agent_type,
            requested,
            epsilon=self.RESEARCH_REVIEWER_READ_STATE_EPSILON,
        )
        if agent_type == "research_reviewer" and self.log_session is not None:
            mode = "READ state.md (consult prior strategy)" if decision else "SKIP state.md (independent assessment)"
            self.log_session.run_log.note(
                f"research_reviewer read_state decision: {mode} "
                f"[read-probability={self.RESEARCH_REVIEWER_READ_STATE_EPSILON}, read_state={bool(decision)}]"
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
            "(does not wait). Use it only for a materially orthogonal idea, not to bypass a blocked "
            "targeted route. The runtime refuses this tool while a completed process audit still needs "
            "curator blocker classification or while a stalled audit names a repeated avoided obligation; "
            "in the latter case, launch the prescribed targeted consolidation instead.\n\n"
            "The worker receives persisted failed-method taboos, active repeated blockers, and an optional "
            "under-explored verified seed. It must choose a distinct bounded claim or record why no such "
            "claim is available. Returns the same shape as SpawnWorker; if no slot is available it returns "
            "spawned=false."

            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda args: self._spawn_free_exploration_tool(manager, args),
        )

    def _spawn_free_exploration_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        del args
        preflight = self._free_exploration_preflight()
        if not preflight.get("allowed"):
            return ToolResult(json.dumps({"spawned": False, **preflight}, ensure_ascii=False))
        constraints = self._free_exploration_constraints()
        payload = manager.spawn_free_exploration_if_available(exploration_constraints=constraints)
        if payload.get("spawned"):
            payload["orthogonality_constraints"] = constraints
        search = getattr(self, "search", None)
        if search is not None and payload.get("spawned") and payload.get("worker_id"):
            search.on_spawn(
                str(payload["worker_id"]),
                FREE_EXPLORATION_WORKER_HINT,
                direction_id=str(payload.get("direction_id") or "free-exploration"),
                gap_id=str(payload.get("gap_id") or "open-exploration"),
            )
        return ToolResult(json.dumps(payload, ensure_ascii=False))

    def _stalled_repeated_obligation(self) -> dict[str, str] | None:
        """Return the latest actionable stalled-audit obligation, if one is persisted."""
        layout = getattr(self, "layout", None)
        state_path = getattr(layout, "progress_audit_state_path", None)
        if state_path is None:
            return None
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        latest = state.get("latest") if isinstance(state, dict) else None
        if not isinstance(latest, dict):
            return None
        if latest.get("status") != "completed" or str(latest.get("verdict") or "").upper() != "STALLED":
            return None
        candidate = latest.get("repeated_avoided_obligation")
        if not isinstance(candidate, dict):
            return None
        direction_id = str(candidate.get("direction_id") or "").strip()
        gap_id = str(candidate.get("gap_id") or "").strip()
        statement = " ".join(str(candidate.get("statement") or "").split())
        if not all((direction_id, gap_id, statement)):
            return None
        return {
            "direction_id": direction_id,
            "gap_id": gap_id,
            "statement": statement,
            "recommended_next_action": " ".join(str(latest.get("recommended_next_action") or "").split()),
        }

    def _free_exploration_preflight(self) -> dict[str, Any]:
        """Prevent free slots from bypassing curation or a stalled, actionable audit obligation."""
        stalled_obligation = self._stalled_repeated_obligation()
        if stalled_obligation is not None:
            direction_id = stalled_obligation["direction_id"]
            gap_id = stalled_obligation["gap_id"]
            statement = stalled_obligation["statement"]
            targeted_attack: dict[str, Any] = {
                "direction_id": direction_id,
                "gap_id": gap_id,
                "consolidation": True,
                "pinned_target": statement,
            }
            attack_instruction = (
                "Launch SpawnWorker with consolidation=true, the returned direction_id/gap_id, and pinned_target "
                "unchanged; either resolve that exact obligation or produce an explicit falsifying witness."
            )
            state_store = getattr(self, "research_state", None)
            preflight = getattr(state_store, "dispatch_preflight", None)
            if callable(preflight):
                try:
                    direct_check = preflight(
                        direction_id=direction_id,
                        gap_id=gap_id,
                        method_id="direct_proof",
                    )
                except ValueError:
                    direct_check = {}
                if isinstance(direct_check, dict) and direct_check.get("reason") == "falsification_required":
                    targeted_attack["method_id"] = "falsification"
                    targeted_attack["pinned_target"] = f"Construct a checkable counterexample to: {statement}"
                    attack_instruction = (
                        "The target has only sampled or incomplete finite evidence. Launch SpawnWorker with "
                        "consolidation=true, method_id='falsification', the returned direction_id/gap_id, and the "
                        "returned pinned_target; construct a checkable counterexample before any proof attempt."
                    )
            return {
                "allowed": False,
                "reason": "stalled_obligation_requires_targeted_attack",
                "direction_id": direction_id,
                "gap_id": gap_id,
                "repeated_avoided_obligation": statement,
                "targeted_attack": targeted_attack,
                "message": (
                    "The latest completed process audit is STALLED and names a repeated avoided obligation. "
                    f"Free exploration would bypass it. {attack_instruction}"
                ),
            }
        if getattr(self, "curator_queue", None) is None:
            return {"allowed": True}
        registry = getattr(self, "blocker_registry", None)
        if registry is None:
            return {
                "allowed": False,
                "reason": "blocker_registry_unavailable",
                "message": "Free exploration is unavailable until persistent blocker state is available.",
            }
        pending = registry.pending_checkpoint_ids()
        if pending:
            return {
                "allowed": False,
                "reason": "blocker_curation_pending",
                "pending_checkpoints": pending,
                "message": (
                    "Completed process audits still require curator blocker classification. Free exploration cannot "
                    "consume worker capacity to bypass this gate; wait for curation, then dispatch an aligned or "
                    "evidence-based orthogonal task."
                ),
            }
        return {"allowed": True}

    def _free_exploration_constraints(self) -> str:
        """Materialize durable taboo and blocker context into a free worker's local task."""
        lines = [
            "# Free Exploration Constraints",
            "Your route must be materially distinct from the failed or blocked approaches below. Do not relabel "
            "a listed approach as novel. If no concrete orthogonal claim is available, record that blocker rather "
            "than proving an unrelated local fact.",
        ]
        state_store = getattr(self, "research_state", None)
        state = state_store.load() if state_store is not None else {}
        directions = state.get("directions") if isinstance(state, dict) else {}
        taboo_entries: list[str] = []
        if isinstance(directions, dict):
            for direction_id, direction in sorted(directions.items()):
                if not isinstance(direction, dict) or direction.get("status") in {"completed", "refuted"}:
                    continue
                for failure in direction.get("failed_methods") or []:
                    if not isinstance(failure, dict):
                        continue
                    family = str(failure.get("method_family") or "").strip()
                    summary = " ".join(str(failure.get("summary") or "").split())
                    if family:
                        taboo_entries.append(
                            f"- `{family}` on `{direction_id}`: {summary[:280] or 'recorded as inadequate.'}"
                        )
        if taboo_entries:
            lines.extend(["", "## Persisted Method Taboos", *taboo_entries[:12]])
        else:
            lines.extend(["", "## Persisted Method Taboos", "- None recorded; still avoid merely restating active routes."])

        registry = getattr(self, "blocker_registry", None)
        registry_state = registry.load() if registry is not None else {}
        blockers = registry_state.get("blockers") if isinstance(registry_state, dict) else {}
        active_blockers = [
            item for item in (blockers or {}).values()
            if isinstance(item, dict) and item.get("status") == "active"
        ]
        if active_blockers:
            lines.extend(["", "## Active Repeated Blockers"])
            for blocker in active_blockers[:6]:
                statement = " ".join(str(blocker.get("statement") or "").split())
                lines.append(f"- `{blocker.get('blocker_id')}`: {statement[:500]}")
        lines.extend([
            "",
            "## Acceptance",
            "- State one exact claim and why it is not a restatement of a listed taboo or blocker.",
            "- A correct but gap-irrelevant lemma is not sufficient; connect it to an open obligation or record why it cannot.",
            "- Do not use a fixed special case as evidence for a universal claim without a proof of the general step.",
        ])
        return "\n".join(lines)

    def _record_research_impact_tool(self, args: dict[str, Any]) -> ToolResult:
        worker_id = str(args.get("worker_id") or "").strip()
        target = self.search.research_target(worker_id)
        if target is None:
            return ToolResult(json.dumps({"error": "unknown worker_id"}), is_error=True)
        direction_id = str(target.get("direction_id") or "").strip()
        if not direction_id:
            return ToolResult(
                json.dumps({"error": "worker has no direction_id; targeted workers must be spawned with direction_id and gap_id"}),
                is_error=True,
            )
        if target.get("impact_status") != "pending":
            return ToolResult(
                json.dumps({"error": f"worker {worker_id} has no pending ResearchImpact assessment"}),
                is_error=True,
            )
        try:
            result = self.research_state.record_impact(
                worker_id=worker_id,
                direction_id=direction_id,
                gap_id=str(target.get("gap_id") or "").strip() or None,
                impact=args,
                route_id=str(target.get("route_id") or "").strip() or None,
            )
            effective_impact = result.get("impact") if isinstance(result.get("impact"), dict) else args
            node = self.search.record_research_impact(worker_id, effective_impact)
            # 禁忌搜索：如果 orchestrator 标注了方法失败类型，记录到方向的 failed_methods 列表
            method_failure_type = str(args.get("method_failure_type") or "").strip() or None
            failed_method_family = str(args.get("failed_method_family") or "").strip() or None
            if method_failure_type and failed_method_family:
                self.research_state.record_method_failure(
                    direction_id=direction_id,
                    method_family=failed_method_family,
                    failure_type=method_failure_type,
                    summary=str(args.get("summary") or ""),
                    gap_id=str(target.get("gap_id") or "").strip() or None,
                )
            self.search.sync_research_state(self.research_state.load())
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        result["state_id"] = node.state_id
        result["impact_status"] = node.impact_status
        rubric_assessment = effective_impact.get("rubric_assessment") if isinstance(effective_impact, dict) else None
        if getattr(self, "layout", None) is not None:
            append_curation_event(
                self.layout,
                "worker_impact_recorded",
                orchestrator_session_id=getattr(self, "session_id", "orchestrator-unknown"),
                context_generation=int(getattr(self, "context_generation", 0)),
                worker_id=worker_id,
                direction_id=direction_id,
                gap_id=str(target.get("gap_id") or "").strip() or None,
                relation_to_target=effective_impact.get("relation_to_target") if isinstance(effective_impact, dict) else None,
                gap_effect=effective_impact.get("gap_effect") if isinstance(effective_impact, dict) else None,
                rubric_passed_count=(rubric_assessment or {}).get("passed_count") if isinstance(rubric_assessment, dict) else None,
                rubric_total_checks=(rubric_assessment or {}).get("total_checks") if isinstance(rubric_assessment, dict) else None,
            )
        return ToolResult(json.dumps(result, ensure_ascii=False))

    def _record_dispatch_constraint_tool(self, args: dict[str, Any]) -> ToolResult:
        try:
            result = self.research_state.record_dispatch_constraint(
                direction_id=str(args.get("direction_id") or ""),
                gap_id=str(args.get("gap_id") or ""),
                claim=str(args.get("claim") or ""),
                status=str(args.get("status") or ""),
                evidence_level=str(args.get("evidence_level") or ""),
                evidence=str(args.get("evidence") or ""),
                source_paths=args.get("source_paths"),
            )
            self.search.sync_research_state(self.research_state.load())
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    def _materialize_process_audit_blocker(
        self,
        payload: dict[str, Any],
        research_state: ResearchStateStore,
    ) -> dict[str, Any] | None:
        del research_state
        audit_state = payload.get("progress_audit")
        latest = audit_state.get("latest") if isinstance(audit_state, dict) else None
        candidate = latest.get("repeated_avoided_obligation") if isinstance(latest, dict) else None
        if not isinstance(candidate, dict):
            return None
        # Process audit supplies a candidate only. Curator owns semantic identity, cross-route grouping,
        # persistence, and resolution in curation_records/blocker_registry.json.
        payload["process_audit_blocker_candidate"] = candidate
        return candidate

    def _sync_research_state_tool(self, args: dict[str, Any]) -> ToolResult:
        try:
            result = self.research_state.sync(args)
            self.search.sync_research_state(self.research_state.load())
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps({"synced": True, **result}, ensure_ascii=False))

    def _register_research_route_tool(self, args: dict[str, Any]) -> ToolResult:
        try:
            result = self.research_state.register_route(
                route_id=str(args.get("route_id") or ""),
                based_on_state_id=str(args.get("based_on_state_id") or ""),
                direction_id=str(args.get("direction_id") or ""),
                gap_id=str(args.get("gap_id") or ""),
                route_claim=str(args.get("route_claim") or ""),
                target=str(args.get("target") or ""),
                success_condition=str(args.get("success_condition") or ""),
                stop_condition=str(args.get("stop_condition") or ""),
                evidence_refs=args.get("evidence_refs"),
            )
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    def _assess_research_route_tool(self, args: dict[str, Any]) -> ToolResult:
        try:
            result = self.research_state.assess_route(
                route_id=str(args.get("route_id") or ""),
                verdict=str(args.get("verdict") or ""),
                summary=str(args.get("summary") or ""),
                evidence_paths=args.get("evidence_paths"),
                close_route=bool(args.get("close_route")),
            )
        except ValueError as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    def _spawn_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        research_state = getattr(self, "research_state", None)
        if research_state is not None and research_state.has_unmigrated_legacy_state():
            return ToolResult(
                json.dumps({
                    "spawned": False,
                    "reason": "research_state_migration_required",
                    "message": (
                        "A non-empty legacy state.md exists without research_state.json. Read it, reconcile multiple "
                        "directions, then call SyncResearchState before spawning so prior strategy is not overwritten."
                    ),
                }, ensure_ascii=False)
            )
        pending_impacts = self.search.pending_impact_worker_ids()
        if pending_impacts:
            return ToolResult(
                json.dumps({
                    "spawned": False,
                    "reason": "pending_research_impact",
                    "pending_worker_ids": pending_impacts,
                    "message": "Call RecordResearchImpact for every pending worker before targeted spawning.",
                }, ensure_ascii=False)
            )
        hint = args.get("hint")
        route_id = str(args.get("route_id") or "").strip() or None
        direction_id = str(args.get("direction_id") or "").strip() or None
        gap_id = str(args.get("gap_id") or "").strip() or None
        method_id = str(args.get("method_id") or "").strip() or None
        if bool(direction_id) != bool(gap_id):
            return ToolResult(
                json.dumps({"error": "direction_id and gap_id must be provided together"}),
                is_error=True,
            )
        # orchestrator 可选地下发一组精选 verified 前沿引用（+一句说明），runtime 会把这些
        # 命题的 Statement 注入 worker 任务，使 worker 无需自调 research_reviewer / 全扫 knowledge。
        raw_frontier = args.get("frontier_refs")
        frontier_refs = (
            [str(item) for item in raw_frontier if str(item).strip()]
            if isinstance(raw_frontier, list)
            else None
        )
        frontier_note = args.get("frontier_note")
        blocker_override_reason = str(args.get("blocker_override_reason") or "").strip()
        # 兑现（consolidation）/对偶（dual）环境：consolidation=true 时钉死目标、禁止弱化，
        # worker 只能「按原样证目标」或「给出显式见证证否」。pinned_target 为钉死命题文本，
        # 缺省则用 hint 表达的目标。默认关闭，完全向后兼容普通 spawn。
        consolidation = bool(args.get("consolidation"))
        allow_weakening = not consolidation
        pinned_target = str(args.get("pinned_target") or "").strip() or None
        rubric = str(args.get("rubric") or "").strip() or None
        # global_attack: 全局 consolidation，直接攻击 problem.md 本身，不绑定任何 direction。
        # 隐含 consolidation=true（禁止弱化）。pinned_target 缺省读 problem.md 全文。
        global_attack = bool(args.get("global_attack"))
        if global_attack:
            consolidation = True
            allow_weakening = False
            if pinned_target is None:
                try:
                    pinned_target = self.layout.read_problem().strip() or None
                except OSError:
                    pinned_target = None
            # global attack 的 hint 完全由代码生成（方法中性），忽略 orchestrator LLM 写的 hint。
            # 原因：orchestrator 的上下文充满某一方法框架（如 anchor-graph）的已验证命题和
            # knowledge 笔记，它写的 hint 会不自觉地把 worker 锚定到该框架。global attack
            # 是对 problem.md 的直接攻击，应让 worker 完全自由选择方法。
            # frontier_refs 同理：如果 orchestrator 传了一堆某框架（如 anchor-graph）的命题，
            # 会无声地把 worker 锁死在那条线上。如果传了 frontier_refs 但没有 frontier_note
            # 明确警告，自动注入降级提示，把 frontier 从"主要前沿"降为"可选参考"。
            hint = GLOBAL_ATTACK_HINT
            if frontier_refs and not frontier_note:
                frontier_note = (
                    "GLOBAL ATTACK — these refs are OPTIONAL background context, NOT a required "
                    "framework. You are free to use ANY method (RSK, Greene, LP dual, construction, "
                    "algebraic, etc.). Do NOT confine yourself to the methods used in these refs."
                )
        # direction 是持久的研究方向标识；SearchGraph 只保存 attempt DAG。显式 parent_ids 支持
        # crossover，多父 attempt 不再与 direction 身份混为一谈。旧 parent_id 仍兼容。
        raw_parent_ids = args.get("parent_ids")
        parent_ids = (
            [str(item) for item in raw_parent_ids if str(item).strip()]
            if isinstance(raw_parent_ids, list)
            else []
        )
        parent_id = args.get("parent_id")
        if parent_id and str(parent_id) not in parent_ids:
            parent_ids.append(str(parent_id))
        search = getattr(self, "search", None)

        # A route scopes a subtree. Resolve its root before preflight so a worker may target a
        # concrete descendant leaf rather than being forced to the route's original parent gap.
        if route_id and research_state is not None:
            route_snapshot = (research_state.load().get("routes") or {}).get(route_id)
            if not isinstance(route_snapshot, dict):
                return ToolResult(json.dumps({"error": f"unknown route_id: {route_id}"}), is_error=True)
            if route_snapshot.get("status") != "active":
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "route_not_active",
                    "route_id": route_id,
                    "status": route_snapshot.get("status"),
                    "message": "Close or replace this route before spawning another worker in its gap subtree.",
                }, ensure_ascii=False))
            if direction_id is None:
                direction_id = str(route_snapshot.get("direction_id") or "").strip() or None
            if gap_id is None:
                gap_id = str(route_snapshot.get("scope_gap_id") or route_snapshot.get("gap_id") or "").strip() or None
            scope_gap_id = str(route_snapshot.get("scope_gap_id") or route_snapshot.get("gap_id") or "").strip()
            if not direction_id or not gap_id or not scope_gap_id or not research_state.gap_is_within_scope(
                direction_id=direction_id, scope_gap_id=scope_gap_id, gap_id=gap_id
            ):
                return ToolResult(
                    json.dumps({"error": "route_id requires the route gap or one of its descendant gaps"}), is_error=True
                )
        elif not global_attack and research_state is not None:
            active_route_ids = [
                str(route.get("route_id"))
                for route in (research_state.load().get("routes") or {}).values()
                if isinstance(route, dict) and route.get("status") == "active"
            ]
            if active_route_ids:
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "active_route_required",
                    "active_route_ids": active_route_ids,
                    "message": "Assign this targeted worker to an active reviewer route before creating or dispatching a gap.",
                }, ensure_ascii=False))

        # Backward compatibility for old orchestrator calls that omit direction_id/gap_id. Prefer
        # inheriting lineage, then an unambiguous canonical target, and finally a visible bootstrap bucket.
        if direction_id is None and research_state is not None:
            inherited = None
            inherited_parent = parent_ids[0] if parent_ids else None
            if inherited_parent and search is not None and inherited_parent in search.graph:
                parent = search.graph.get(str(inherited_parent))
                if parent.direction_id and parent.gap_id:
                    inherited = (parent.direction_id, parent.gap_id)
            inferred = inherited or research_state.default_target()
            direction_id, gap_id = inferred or ("bootstrap-direction", "bootstrap-gap")

        # global_attack: 全局攻击 problem.md，不进 per-direction 体系。
        # 用固定占位 direction_id 便于日志追踪，但不调 ensure_target、不进 hard gate。
        if global_attack:
            direction_id = "global-problem-attack"
            gap_id = "global-problem-attack"
        elif direction_id and research_state is not None:
            try:
                research_state.ensure_target(direction_id=direction_id, gap_id=gap_id, hint=hint)
                if search is not None:
                    search.sync_research_state(research_state.load())
            except ValueError as exc:
                return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        # 结构化反证 / 有限数值证据 gate：无论普通 spawn 还是 consolidation 都不得绕过一个
        # 已被明确反驳的 exact gap；仅有采样证据的猜想必须先走专门的 falsification worker。
        if not global_attack and research_state is not None and direction_id and gap_id:
            try:
                preflight = research_state.dispatch_preflight(
                    direction_id=direction_id,
                    gap_id=gap_id,
                    method_id="consolidation" if consolidation else method_id,
                    blocker_override_reason=blocker_override_reason,
                )
            except ValueError as exc:
                return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
            if not preflight.get("allowed"):
                return ToolResult(json.dumps({
                    "spawned": False,
                    **preflight,
                    "direction_id": direction_id,
                    "gap_id": gap_id,
                }, ensure_ascii=False))
            curated_preflight = self.blocker_registry.dispatch_preflight(
                direction_id=direction_id,
                gap_id=gap_id,
                blocker_override_reason=blocker_override_reason,
                require_curation=self.curator_queue is not None,
            )
            if not curated_preflight.get("allowed"):
                return ToolResult(json.dumps({
                    "spawned": False,
                    **curated_preflight,
                    "direction_id": direction_id,
                    "gap_id": gap_id,
                }, ensure_ascii=False))
            curated_blockers = curated_preflight.get("blockers") or []
            direct_curated = [
                item for item in curated_blockers
                if isinstance(item, dict) and item.get("gate", {}).get("gap_id") == gap_id
            ]
            if direct_curated:
                blocker = direct_curated[0]
                approaches = blocker.get("approaches") if isinstance(blocker.get("approaches"), list) else []
                approach_lines = "\n".join(
                    f"- {item.get('direction_id')}/{item.get('gap_id')}/{item.get('method_id')}: "
                    f"{item.get('description')}"
                    for item in approaches if isinstance(item, dict)
                )
                hint = (
                    "# Active Curated Blocker\n"
                    f"Persistent blocker `{blocker.get('blocker_id')}` appeared {blocker.get('occurrence_count')} time(s).\n"
                    f"Your only useful target is this unresolved obligation:\n{blocker.get('statement') or 'not stated'}\n\n"
                    "Routes that already encountered it:\n"
                    f"{approach_lines or '- No route detail recorded.'}\n\n"
                    "Do not merely restate it, assume it, or prove lemmas that avoid it. Either resolve this exact "
                    "obligation, prove a direct refutation with a checkable witness, or record the precise remaining "
                    "sub-obligation through RecordDifficulty.\n\n"
                    + str(hint or "")
                )
            elif blocker_override_reason and curated_blockers:
                hint = (
                    "# Explicit Curated-Blocker Pivot\n"
                    f"Active blocker: {curated_blockers[0].get('statement') or 'not stated'}\n"
                    f"Pivot evidence: {blocker_override_reason}\n\n"
                    + str(hint or "")
                )
            # Legacy state blockers remain readable to avoid dropping an already persisted gate during upgrade.
            blocker = research_state.active_audit_blocker(direction_id)
            if isinstance(blocker, dict) and blocker.get("status") == "active":
                if blocker.get("gap_id") == gap_id:
                    blocker_statement = str(blocker.get("statement") or "").strip()
                    hint = (
                        "# Active Legacy Process-Audit Blocker\n"
                        f"Your only useful target is this unresolved obligation:\n{blocker_statement}\n\n"
                        "Do not merely restate it, assume it, or prove lemmas that avoid it. Either resolve this exact "
                        "obligation, prove a direct refutation with a checkable witness, or record the precise remaining "
                        "sub-obligation through RecordDifficulty.\n\n"
                        + str(hint or "")
                    )
                elif blocker_override_reason:
                    hint = (
                        "# Explicit Legacy-Blocker Pivot\n"
                        f"Active blocker: {blocker.get('statement') or 'not stated'}\n"
                        f"Pivot evidence: {blocker_override_reason}\n\n"
                        + str(hint or "")
                    )
        # 硬 gate：若本次不是 consolidation，且目标方向正处于"该对偶(dual_probe_required)"或
        # "该送审(consolidation_ready)"状态，则拒绝这次普通 targeted spawn，强制下一枪走 consolidation。
        # 只针对被瞄准的那条方向做拦截（surgical），不影响对其它方向的正常 spawn。
        if not consolidation and research_state is not None and direction_id:
            direction_snapshot: dict[str, Any] = {}
            try:
                state_snapshot = research_state.load()
                direction_snapshot = (state_snapshot.get("directions") or {}).get(direction_id, {})
                st_summary = research_state.summary(state_snapshot)
                dual_ids = set(st_summary.get("dual_probe_required_directions") or [])
                ready_ids = {
                    str(entry.get("direction_id"))
                    for entry in (st_summary.get("consolidation_ready_directions") or [])
                }
            except Exception:
                dual_ids, ready_ids = set(), set()
            if direction_id in dual_ids:
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "dual_probe_required",
                    "direction_id": direction_id,
                    "message": (
                        f"Direction '{direction_id}' had a consolidation attempt hit a wall and is flagged for a "
                        "dual/falsification probe. Re-issue SpawnWorker with consolidation=true and pinned_target set "
                        "to the NEGATION of this direction's target (construct an explicit witness / counterexample, "
                        "small cases first). No ordinary worker may be spawned into this direction until the probe runs."
                    ),
                }, ensure_ascii=False))
            if direction_id in ready_ids:
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "consolidation_required",
                    "direction_id": direction_id,
                    "message": (
                        f"Direction '{direction_id}' has accumulated >= threshold new verified props since its last "
                        "consolidation submission. Before more incremental work, re-issue SpawnWorker with "
                        "consolidation=true and pinned_target set to this direction's terminal goal, attacking it "
                        "head-on with no weakening. Give the props on the path to that goal via frontier_refs."
                    ),
                }, ensure_ascii=False))
            blocked_status = str(direction_snapshot.get("status") or "active")
            if blocked_status in {"paused", "stalled", "refuted", "completed"} or bool(
                direction_snapshot.get("deprioritized")
            ):
                return ToolResult(json.dumps({
                    "spawned": False,
                    "reason": "direction_not_schedulable",
                    "direction_id": direction_id,
                    "status": blocked_status,
                    "deprioritized": bool(direction_snapshot.get("deprioritized")),
                    "message": "Ordinary workers are blocked for this direction; revise the research state or use a consolidation/dual probe.",
                }, ensure_ascii=False))
        if consolidation:
            method_id = "consolidation"
        elif not method_id:
            method_id = "direct_proof"
        route_state = research_state.load() if research_state is not None else {}
        active_routes = [
            route for route in (route_state.get("routes") or {}).values()
            if isinstance(route, dict) and route.get("status") == "active"
        ]
        if active_routes and not route_id and not global_attack:
            return ToolResult(
                json.dumps({
                    "spawned": False,
                    "reason": "active_route_required",
                    "active_route_ids": [route.get("route_id") for route in active_routes],
                    "message": "Assign this targeted worker to an active reviewer route, or assess/close those routes first.",
                }, ensure_ascii=False)
            )
        if route_id and research_state is not None:
            route = (route_state.get("routes") or {}).get(route_id)
            if not isinstance(route, dict):
                return ToolResult(json.dumps({"error": f"unknown route_id: {route_id}"}), is_error=True)
            route_direction_id = str(route.get("direction_id") or "").strip() or None
            route_scope_gap_id = str(route.get("scope_gap_id") or route.get("gap_id") or "").strip() or None
            if direction_id is None:
                direction_id = route_direction_id
            if gap_id is None:
                gap_id = route_scope_gap_id
            if direction_id != route_direction_id or not route_scope_gap_id or not gap_id or not research_state.gap_is_within_scope(
                direction_id=direction_id or "", scope_gap_id=route_scope_gap_id, gap_id=gap_id
            ):
                return ToolResult(
                    json.dumps({"error": "route_id requires the route gap or one of its descendant gaps"}), is_error=True
                )
        payload = manager.spawn(
            hint,
            route_id=route_id,
            direction_id=direction_id,
            gap_id=gap_id,
            method_id=method_id,
            frontier_refs=frontier_refs,
            frontier_note=frontier_note,
            allow_weakening=allow_weakening,
            pinned_target=pinned_target,
            rubric=rubric,
        )
        # selection 层是纯建议性的只读附加信号；若未初始化（如绕过 __init__ 的单测），
        # 直接跳过记账，绝不影响 SpawnWorker 的真实语义。on_spawn 对无效 parent_id 会
        # 静默回退 root，不会把异常冒回主流程。
        if search is not None and payload.get("spawned") and payload.get("worker_id"):
            search.on_spawn(
                str(payload["worker_id"]),
                hint,
                parent_id=str(parent_id) if parent_id else None,
                parent_ids=parent_ids,
                route_id=route_id,
                direction_id=direction_id,
                gap_id=gap_id,
                method_id=method_id,
            )
            if route_id and research_state is not None:
                try:
                    research_state.attach_worker_to_route(
                        route_id=route_id,
                        worker_id=str(payload["worker_id"]),
                        direction_id=direction_id,
                        gap_id=gap_id,
                        method_id=method_id,
                    )
                except ValueError as exc:
                    return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        # 送审日志：consolidation=true 的 spawn 记一条"送审"到 state；返回结果由
        # RecordResearchImpact 按 worker_id 回填。若该方向此前被撞墙置了 dual_probe_required，
        # 本次记为对偶探针(dual_probe)。纯记账，绝不影响 SpawnWorker 的真实语义。
        if consolidation and research_state is not None and payload.get("spawned") and payload.get("worker_id"):
            try:
                if global_attack:
                    # 全局攻击 problem.md：只更新全局 consolidation 基线，不进 per-direction 日志。
                    research_state.record_global_consolidation(worker_id=str(payload["worker_id"]))
                else:
                    snapshot = research_state.load().get("directions", {}).get(direction_id or "", {})
                    research_state.log_consolidation_submission(
                        direction_id=direction_id,
                        gap_id=gap_id,
                        worker_id=str(payload["worker_id"]),
                        pinned_target=pinned_target or hint,
                        dual_probe=bool(snapshot.get("dual_probe_required")),
                    )
            except Exception:
                pass
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            stop_agent=manager.solved_result is not None,
            stop_answer=_solution_final_answer(manager.solution_path),
        )

    def _difficulty_portfolio_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Surface unresolved worker difficulties and obtain bounded reviewer comparison.

        The runtime only packages evidence and invokes the independent reviewer for a
        multi-attempt comparison. It does not decide blocker identity: that remains the
        curator's checkpoint-time responsibility.
        """
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
        result: dict[str, Any] = {
            "candidate_worker_ids": [item["worker_id"] for item in handoffs],
            "handoffs": handoffs,
            "message": (
                "These are worker-level difficulty candidates reconciled only against their own final trail. "
                "Do not treat matching wording as a canonical blocker; compare exact obligations and evidence."
            ),
        }
        if len(handoffs) < 2:
            result["review_status"] = "awaiting_comparable_attempt"
            return result

        batch_key = review_batch_key(handoffs)
        reports = getattr(self, "_difficulty_review_reports", {})
        handled = getattr(self, "_reviewed_difficulty_batches", set())
        if batch_key not in handled:
            reports[batch_key] = self._run_difficulty_research_reviewer(handoffs)
            handled.add(batch_key)
            self._difficulty_review_reports = reports
            self._reviewed_difficulty_batches = handled
        result["review_status"] = "reviewed" if reports.get(batch_key) else "review_unavailable"
        result["research_reviewer_report"] = reports.get(batch_key, "")
        return result

    def _run_difficulty_research_reviewer(self, handoffs: list[dict[str, Any]]) -> str:
        service = getattr(self, "_reset_reviewer_service", None)
        if service is None:
            return (
                "Research reviewer service is unavailable. The orchestrator must compare the structured handoffs "
                "and defer canonical blocker identity to curator curation."
            )
        read_state = self._reviewer_read_state_resolver("research_reviewer", None)
        state_directive = (
            "[read_state=true] You MAY consult verified_propositions/**/state.md as a fallible strategy view."
            if read_state else
            "[read_state=false] Do NOT read verified_propositions/**/state.md; inspect cited proofs and evidence directly."
        )
        try:
            return service.call(
                "research_reviewer",
                "Compare unresolved worker difficulty handoffs",
                state_directive + "\n\n" + build_difficulty_reviewer_prompt(handoffs),
            )
        except Exception as exc:
            return (
                "Automatic difficulty comparison failed; do not infer common blocker identity. "
                f"Failure: {type(exc).__name__}: {exc}"
            )

    def _wait_tool(
        self,
        manager: WorkerManager,
        args: dict[str, Any],
    ) -> ToolResult:
        search = getattr(self, "search", None)
        timeout_seconds = args.get("seconds")
        payload = manager.wait(timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None)
        difficulty_portfolio = self._difficulty_portfolio_payload(payload)
        if difficulty_portfolio is not None:
            payload["difficulty_portfolio"] = difficulty_portfolio
        # 兑现/对偶信号（surface）：每次 TaskOutput 都把"该送审 / 该对偶"的方向暴露给 orchestrator，
        # 与 _spawn_tool 的硬 gate 配套，使 orchestrator 既看得到、也绕不过。
        research_state = getattr(self, "research_state", None)
        if research_state is not None:
            try:
                # 自动同步 knowledge 中的 INVALIDATED 标注到 gap status，
                # 防止 worker 在已废方向上继续工作。
                knowledge_dir = self.layout.workspace_dir / "knowledge"
                inv_result = research_state.sync_invalidated_from_knowledge(knowledge_dir)
                if inv_result.get("synced"):
                    payload["knowledge_invalidation_sync"] = inv_result
                self._materialize_process_audit_blocker(payload, research_state)

                st_summary = research_state.summary()
                dual = st_summary.get("dual_probe_required_directions") or []
                ready = st_summary.get("consolidation_ready_directions") or []
                if dual or ready:
                    payload["consolidation_directive"] = {
                        "dual_probe_required_directions": dual,
                        "consolidation_ready_directions": ready,
                        "message": (
                            "Action required before pouring more normal workers into these directions. "
                            "Directions in dual_probe_required_directions take priority: a prior consolidation hit a "
                            "wall, so spawn `SpawnWorker consolidation=true` pinning the NEGATION of the target "
                            "(construct a witness / counterexample, small cases first). Directions in "
                            "consolidation_ready_directions have accumulated >= threshold new verified props since "
                            "their last submission: spawn `SpawnWorker consolidation=true` pinning their terminal "
                            "goal and attack it head-on (no weakening). The runtime hard-gates ordinary targeted "
                            "spawns into these directions until a consolidation attempt is made."
                        ),
                    }
                # 全局 consolidation 信号（软引导）：当全局 verified props 总数自上次全局
                # consolidation 以来增量 >= GLOBAL_CONSOLIDATION_EVERY_N_PROPS，建议 orchestrator
                # 发起一次直接攻击 problem.md 本身的 consolidation worker（不是 per-direction 的）。
                global_status = st_summary.get("global_consolidation") or {}
                if global_status.get("ready"):
                    payload["global_consolidation_directive"] = {
                        "ready": True,
                        "current_verified_props": global_status.get("current_verified_props"),
                        "delta": global_status.get("delta"),
                        "threshold": global_status.get("threshold"),
                        "last_consolidation_count": global_status.get("last_consolidation_count"),
                        "message": (
                            f"Global consolidation is ready: {global_status.get('delta')} new verified propositions "
                            f"have accumulated since the last global consolidation attempt "
                            f"(threshold={global_status.get('threshold')}). Launch a SpawnWorker with "
                            "`consolidation=true` and `global_attack=true` to attack `problem.md` directly — "
                            "not any specific direction's terminal goal. The runtime will read problem.md into "
                            "`pinned_target` automatically. Provide the most relevant verified propositions via "
                            "`frontier_refs`. This is advisory; judge whether the accumulated props are mature "
                            "enough to warrant a head-on attack on the full problem."
                        ),
                    }
            except Exception:
                pass
        # 只读附加：调度层 selection 建议（design §3/§5）。不改变 worker 真实执行语义；
        # search 未初始化时整段跳过。
        if search is not None:
            # --- consolidation worker 撞墙自动降权 ---
            # 对未完成 pinned_target 的 consolidation worker，代码直接记录 wall/blocked_gap，
            # 不依赖 orchestrator 调 RecordResearchImpact。这确保 rejected 的 consolidation
            # worker 也能正确触发降权/对偶探针。
            self._auto_assess_consolidation_workers(payload, research_state)
            for completed in payload.get("completed", []) or []:
                status = str(completed.get("status") or "").lower()
                direction_id = str(completed.get("direction_id") or "").strip()
                if (
                    research_state is not None
                    and direction_id
                    and not direction_id.startswith("free-exploration-")
                    and direction_id != "global-problem-attack"
                    and status in {"failed", "error", "unverified", "rejected"}
                ):
                    try:
                        research_state.record_worker_outcome(
                            worker_id=str(completed.get("worker_id") or ""),
                            direction_id=direction_id,
                            gap_id=str(completed.get("gap_id") or "").strip() or None,
                            method_id=str(completed.get("method_id") or "").strip() or None,
                            route_id=str(completed.get("route_id") or "").strip() or None,
                            status=status,
                            failure_kind=str(completed.get("failure_kind") or "").strip() or None,
                            summary=str(completed.get("summary") or ""),
                        )
                    except ValueError:
                        pass
                search.on_worker_result(completed)
            if research_state is not None:
                search.sync_research_state(research_state.load())
            advice = search.advise()
            payload["selection_advice"] = advice
            pending_impacts = search.pending_impact_worker_ids()
            if pending_impacts:
                payload["research_impact_required"] = {
                    "worker_ids": pending_impacts,
                    "message": (
                        "Assess each worker with RecordResearchImpact before targeted spawning. "
                        "Read its theorem_check_file and compare the final Statement with its direction-level gap."
                    ),
                }
            # 只读观测：每个 selection cycle 把搜索树快照落到独立日志。
            if self._search_tree_sink is not None:
                self._search_tree_sink.snapshot(search)
        process_reset = self._handle_process_audit_reset(payload, manager)
        if process_reset is not None:
            payload["process_audit_context_reset"] = process_reset
        if manager.solved_result is not None:
            manager.close()
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            stop_agent=manager.solved_result is not None,
            stop_answer=_solution_final_answer(manager.solution_path),
        )

    def _handle_process_audit_reset(
        self,
        payload: dict[str, Any],
        manager: WorkerManager,
    ) -> dict[str, Any] | None:
        audit_state = payload.get("progress_audit")
        latest = audit_state.get("latest") if isinstance(audit_state, dict) else None
        if not isinstance(latest, dict) or not latest.get("context_reset_required"):
            return None
        checkpoint_id = str(latest.get("checkpoint_id") or "").strip()
        if not checkpoint_id or latest.get("context_reset_handled"):
            return None
        handled = getattr(self, "_handled_process_audit_resets", set())
        if checkpoint_id in handled:
            return None

        audit_path = str(latest.get("audit_path") or "").strip()
        audit_queue = getattr(manager, "progress_audit_queue", None)
        # Do not persist handled=true until the reset boundary has actually run the mandatory reviewer.
        # If the process exits between TaskOutput and the next model request, a later process can retry it.
        handled.add(checkpoint_id)
        self._handled_process_audit_resets = handled
        previous_generation = int(getattr(self, "context_generation", 0))
        self.context_generation = previous_generation + 1
        append_curation_event(
            self.layout,
            "orchestrator_context_reset",
            orchestrator_session_id=getattr(self, "session_id", "orchestrator-unknown"),
            previous_context_generation=previous_generation,
            context_generation=self.context_generation,
            checkpoint_id=checkpoint_id,
            audit_path=audit_path,
            verdict=latest.get("verdict"),
            terminal_gap=latest.get("terminal_gap"),
        )
        transition_path = self.layout.progress_audits_dir / checkpoint_id / "strategy_transition.md"
        transition_path.parent.mkdir(parents=True, exist_ok=True)
        transition_path.write_text(
            "# Orchestrator Strategy Transition\n\n"
            "## Process Trigger\n"
            f"- Process audit: `{audit_path}`\n"
            f"- Verdict: `{latest.get('verdict') or 'unknown'}`\n"
            f"- Terminal gap: {latest.get('terminal_gap') or 'not stated'}\n"
            "- Reason: cumulative no-progress threshold reached; the prior orchestrator context was cleared.\n\n"
            "## Required Comparison for Later Outcomes\n"
            "- Compare the next context generation's targets, rubrics, summaries, and impact classifications with the pre-reset portfolio.\n"
            "- Determine whether it materially changed the strategy or merely repeated the stalled route under new wording.\n"
            "- Do not treat the reset itself as mathematical progress.\n",
            encoding="utf-8",
        )
        curator_queue = getattr(manager, "curator_queue", None)
        if curator_queue is not None:
            from .curator import CuratorTask
            curator_queue.submit(
                CuratorTask(
                    trace_segment=[],
                    source_label=f"strategy-transition/{checkpoint_id}",
                    task_kind="strategy_transition",
                    artifact_path=transition_path,
                )
            )

        directive = (
            "# Process Audit Context Reset\n\n"
            "Begin a fresh orchestrator session. Your prior orchestration conversation has been cleared because the "
            "independent process auditor recorded cumulative non-progress at "
            f"checkpoint `{checkpoint_id}`; its verdict was `{latest.get('verdict') or 'unknown'}`.\n\n"
            f"- Terminal gap: {latest.get('terminal_gap') or 'not stated'}\n"
            f"- Process audit: `{audit_path}`\n\n"
            "The runtime will now obtain a mandatory independent `research_reviewer` assessment and inject it below. "
            "Treat that report as decision support, verify its evidence, call `SyncResearchState` before targeted spawning, "
            "and do not resume the previously stalled direction without new cited evidence."
        )
        self._pending_context_reset = {
            "directive": directive,
            "checkpoint_id": checkpoint_id,
            "audit_path": audit_path,
            "terminal_gap": str(latest.get("terminal_gap") or ""),
            "verdict": str(latest.get("verdict") or ""),
            "audit_queue": audit_queue,
        }
        return {
            "checkpoint_id": checkpoint_id,
            "audit_path": audit_path,
            "message": (
                "Process audit scheduled a fresh orchestrator context; a mandatory research_reviewer assessment will run "
                "immediately before its next model request."
            ),
        }

    def _consume_process_audit_context_reset(self) -> str | None:
        ticket = getattr(self, "_pending_context_reset", None)
        self._pending_context_reset = None
        if not ticket:
            return None
        if isinstance(ticket, str):
            return ticket
        directive = str(ticket.get("directive") or "").strip()
        if not directive:
            return None
        reviewer_report = self._run_reset_research_reviewer(ticket)
        audit_queue = ticket.get("audit_queue")
        checkpoint_id = str(ticket.get("checkpoint_id") or "").strip()
        if audit_queue is not None and checkpoint_id:
            try:
                audit_queue.mark_context_reset_handled(checkpoint_id)
            except Exception:
                pass
        return directive + "\n\n## Mandatory Fresh Research Reviewer Assessment\n\n" + reviewer_report

    def _run_reset_research_reviewer(self, ticket: dict[str, Any]) -> str:
        """Run exactly one independent portfolio review at the boundary of a reset context."""
        service = getattr(self, "_reset_reviewer_service", None)
        checkpoint_id = str(ticket.get("checkpoint_id") or "unknown")
        audit_path = str(ticket.get("audit_path") or "")
        terminal_gap = str(ticket.get("terminal_gap") or "not stated")
        verdict = str(ticket.get("verdict") or "unknown")
        if service is None:
            return (
                "Reviewer service was unavailable while consuming this reset ticket. Read the cited process audit and "
                "treat the portfolio as requiring a fresh independent review before targeted spawning."
            )
        read_state = self._reviewer_read_state_resolver("research_reviewer", None)
        state_directive = (
            "[read_state=true] You MAY consult verified_propositions/**/state.md as a stale hypothesis."
            if read_state else
            "[read_state=false] Do NOT read verified_propositions/**/state.md; independently inspect verified proofs, "
            "knowledge, reviewer-history, and the cited audit."
        )
        prompt = (
            f"{state_directive}\n\n"
            "Run a mandatory fresh portfolio assessment after a process-audit context reset. Do not continue the old "
            "orchestrator conversation or merely paraphrase its recommendation. Identify the current verified position, "
            "the true terminal gap, stale state that conflicts with verified proofs, and exactly one best next mathematical "
            "target. Apply your required adversarial review and any decision-relevant numerical check.\n\n"
            f"- Reset checkpoint: `{checkpoint_id}`\n"
            f"- Audit verdict: `{verdict}`\n"
            f"- Audit terminal gap: {terminal_gap}\n"
            f"- Process audit path: `{audit_path}`\n"
        )
        try:
            return service.call(
                "research_reviewer",
                "Fresh portfolio review after audit reset",
                prompt,
            )
        except Exception as exc:
            return (
                "Automatic research_reviewer invocation failed; do not assume the old strategy remains valid. "
                f"Failure: {type(exc).__name__}: {exc}"
            )

    def _auto_assess_consolidation_workers(
        self, payload: dict[str, Any], research_state: Any
    ) -> None:
        """对未完成 pinned_target 的 consolidation worker，代码直接记录降权。

        - rejected consolidation worker → consolidation_outcome=wall（撞墙）
        - verified 但 target_achieved=False → consolidation_outcome=wall（弱化/未命中目标）
        - verified 但 target_achieved=None → 不处理，留给 orchestrator 评估
          （verifier 通过了，需要 LLM 判断是否真正完成了目标）
        - target_achieved=True → 不处理（成功）

        记录后该 worker 不会出现在 pending_research_impacts 中，避免 orchestrator 重复评估。
        """
        if research_state is None:
            return
        for completed in payload.get("completed", []) or []:
            if not completed.get("is_consolidation"):
                continue
            target_achieved = completed.get("target_achieved")
            if target_achieved is True:
                continue  # 成功，不干预
            if target_achieved is None:
                continue  # 需要 orchestrator 判断，不干预
            # target_achieved is False → 明确未完成，代码直接记录
            worker_id = str(completed.get("worker_id") or "")
            direction_id = str(completed.get("direction_id") or "")
            gap_id = str(completed.get("gap_id") or "") or None
            if not worker_id or not direction_id:
                continue
            # global attack 失败：基线已在 spawn 时更新（record_global_consolidation），
            # 不进 per-direction 降权体系（没有 dual_probe / deprioritized 概念）。
            # 只标记 auto_assessed，避免 orchestrator 重复评估。
            if direction_id == "global-problem-attack":
                completed["auto_assessed"] = True
                completed["consolidation_outcome"] = "wall"
                continue
            status = str(completed.get("status") or "")
            verify_history = completed.get("verify_history") or []
            # 构建摘要
            review_excerpts = []
            for vh in verify_history:
                verdict = vh.get("verdict", "?")
                excerpt = (vh.get("review_excerpt") or "")[:300]
                review_excerpts.append(f"[round {vh.get('round','?')} verdict={verdict}] {excerpt}")
            summary_text = (
                f"Consolidation worker {worker_id} (direction={direction_id}) "
                f"did not achieve its pinned target. status={status}. "
                f"Verify history: {' | '.join(review_excerpts) if review_excerpts else 'no verify rounds recorded'}"
            )
            try:
                research_state.record_impact(
                    worker_id=worker_id,
                    direction_id=direction_id,
                    gap_id=gap_id,
                    route_id=str(completed.get("route_id") or "").strip() or None,
                    impact={
                        "relation_to_target": "weaker_than_target" if status == "verified" else "unrelated",
                        "gap_effect": "unchanged",
                        "continuation_value": "low",
                        "summary": summary_text,
                        "consolidation_outcome": "wall",
                        "verified_output": status == "verified",
                        "new_gaps": [],
                    },
                )
            except Exception:
                pass
            # 标记 payload，让 orchestrator 知道这个 worker 已被代码自动评估
            completed["auto_assessed"] = True
            completed["consolidation_outcome"] = "wall"
            completed["failure_kind"] = "consolidation_wall"

    def _task(self) -> str:
        hint = self.layout.read_hint()
        parts = [
            "Use workers to solve the problem stated in `problem.md`.",
            f"Maximum concurrent workers: {self.max_workers}",
        ]
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
        "route_id": result.route_id,
        "direction_id": result.direction_id,
        "gap_id": result.gap_id,
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
    # rejected worker 的失败反馈：verify_history（LLM 结果总结统一走 result_summary_file）
    # （对 consolidation worker 也适用，上面已设 target_achieved）
    if result.status == "rejected":
        if result.verify_history:
            payload["verify_history"] = result.verify_history
        payload["unverified_dir"] = str(result.worker_dir)
    # worker 完成后的统一 LLM 结果总结（verified/rejected 共用同一份文件，已核对过
    # generator 的原始困难声明是否过时）
    if result.result_summary_file:
        payload["result_summary_file"] = str(result.result_summary_file)
    # generator 产出的困难声明：worker 回避了什么数学困难、为什么回避、尝试过但失败的路线
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
