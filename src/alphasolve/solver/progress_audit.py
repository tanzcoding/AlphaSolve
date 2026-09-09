"""Persisted, blocking strategic audits for AlphaSolve research proposals and outcomes."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import Agent, Workspace

from .curation_records import read_recent_events, relative_path
from .tool_runtime import build_solver_tool_registry
from .workspace_access import RoleWorkspaceAccess
from .research_frontier_state import write_research_frontier_state

if TYPE_CHECKING:
    from .client_factory import ClientFactory
    from .curator import CuratorQueue
    from .logging.log_session import LogSession
    from .project import ProjectLayout


ProgressAuditRunner = Callable[[Path, str], str]
_VALID_VERDICTS = {"ADVANCING", "STALLED", "MISALIGNED", "INSUFFICIENT_EVIDENCE"}


@dataclass(frozen=True)
class ProgressAuditTask:
    checkpoint_id: str
    watermark: int
    previous_watermark: int
    evidence_path: Path
    checkpoint_dir: Path


class ProgressAuditQueue:
    """Build immutable audit evidence and expose long-horizon outcome-audit decisions.

    Worker execution records outcomes first. The worker threads remain independent, but
    every ``outcomes_per_audit`` settled outcomes trigger one composite portfolio audit
    that judges whether the accumulated evidence is actually advancing `problem.md`.
    This is the only audit this queue performs: it never gates an individual reviewer
    proposal before execution, only the executed portfolio afterward.
    """

    DEFAULT_OUTCOMES_PER_AUDIT = 5
    # Consecutive STALLED/MISALIGNED outcome-audit checkpoints citing the same
    # `repeated_avoided_obligation` before the runtime marks the next proposal
    # `stagnation_level=FORCE_PIVOT` (configurable via `stagnation_force_pivot_streak`).
    DEFAULT_STAGNATION_FORCE_PIVOT_STREAK = 4

    def __init__(
        self,
        *,
        layout: "ProjectLayout",
        suite: Any,
        client_factory: "ClientFactory",
        curator_queue: "CuratorQueue | None" = None,
        execution_gateway: Any | None = None,
        log_session: "LogSession | None" = None,
        stop_event: threading.Event | None = None,
        outcomes_per_audit: int | None = None,
        stagnation_force_pivot_streak: int | None = None,
        audit_runner: ProgressAuditRunner | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.curator_queue = curator_queue
        self.execution_gateway = execution_gateway
        self.log_session = log_session
        self.stop_event = stop_event
        self.outcomes_per_audit = max(1, int(outcomes_per_audit or self.DEFAULT_OUTCOMES_PER_AUDIT))
        self.stagnation_force_pivot_streak = max(
            2, int(stagnation_force_pivot_streak or self.DEFAULT_STAGNATION_FORCE_PIVOT_STREAK)
        )
        self.audit_runner = audit_runner
        self._queue: queue.Queue[ProgressAuditTask | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="progress-audit")
        self._lock = threading.RLock()
        self._decision_ready = threading.Condition(self._lock)
        self._started = False
        self._state = self._load_state()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._schedule_recovered_targeted_verified_checkpoint_locked()
            for checkpoint_id in self._state.get("pending_checkpoints") or []:
                task = self._restore_pending_task(str(checkpoint_id))
                if task is not None:
                    self._queue.put(task)
            self._thread.start()

    def _schedule_recovered_targeted_verified_checkpoint_locked(self) -> None:
        """Recover missed direct DAG curation without scheduling a process audit."""
        last_scheduled = int(self._state.get("last_scheduled_outcome") or 0)
        outcomes = _read_jsonl(self.layout.progress_audit_outcomes_path)
        missed = [
            item
            for item in outcomes
            if int(item.get("sequence") or 0) > last_scheduled
            and str(item.get("status") or "") == "verified"
            and str(item.get("difficulty_id") or "").strip()
        ]
        if not missed:
            return
        watermark = max(int(item.get("sequence") or 0) for item in outcomes)
        self._submit_evidence_checkpoint_locked(
            checkpoint_id=f"targeted-verified-recovery-{last_scheduled + 1:04d}-{watermark:04d}",
            previous_watermark=last_scheduled,
            watermark=watermark,
            trigger_reason="recovered_targeted_verified_evidence",
        )

    def _submit_evidence_checkpoint_locked(
        self,
        *,
        checkpoint_id: str,
        previous_watermark: int,
        watermark: int,
        trigger_reason: str,
    ) -> None:
        """Persist and submit direct DAG curation evidence without a process audit."""
        checkpoint_dir = self.layout.workspace_dir / "curation_records" / "evidence_checkpoints" / checkpoint_id
        ready_path = checkpoint_dir / "curation_ready.json"
        if ready_path.is_file():
            return
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        task = ProgressAuditTask(
            checkpoint_id=checkpoint_id,
            watermark=watermark,
            previous_watermark=previous_watermark,
            evidence_path=checkpoint_dir / "curation_input.json",
            checkpoint_dir=checkpoint_dir,
        )
        curation_input_path = _write_curation_input(layout=self.layout, task=task)
        _write_json(ready_path, {
            "checkpoint_id": checkpoint_id,
            "status": "ready",
            "trigger_reason": trigger_reason,
            "watermark": watermark,
            "previous_watermark": previous_watermark,
            "curation_input_path": _relative_to_workspace(curation_input_path, self.layout.workspace_dir),
            "created_at": _now_iso(),
        })
        brief_path = _write_evidence_checkpoint_brief(
            layout=self.layout,
            checkpoint_id=checkpoint_id,
            curation_input_path=curation_input_path,
            trigger_reason=trigger_reason,
        )
        if self.curator_queue is not None:
            from .curator import CuratorTask
            self.curator_queue.submit(
                CuratorTask(
                    trace_segment=[],
                    source_label=f"targeted-verified-evidence/{checkpoint_id}",
                    task_kind="evidence_checkpoint",
                    artifact_path=brief_path,
                )
            )

    def stop(self, timeout: float = 60.0) -> None:
        with self._lock:
            started = self._started
        if not started:
            return
        self._queue.put(None)
        self._thread.join(timeout=timeout)

    def record_outcome(self, payload: dict[str, Any]) -> bool:
        """Persist one terminal worker result and schedule an audit when due."""
        recorded, _checkpoint_id = self._record_outcome(payload)
        return recorded

    def record_outcomes(self, payloads: list[dict[str, Any]]) -> list[str]:
        """Persist a TaskOutput batch and return any checkpoint IDs it created.

        Process audit is a long-horizon composite judgement: whether the portfolio has
        made real progress, whether the research plan is complete, and whether the plan
        itself is wrong. That question is only answerable over an accumulated window, so
        it stays on the ``outcomes_per_audit`` cadence owned by ``_record_outcome``.

        A ``TaskOutput`` batch is *not* such a window. Gating every batch made the audit
        fire per dispatch round, which both destroyed the sampling interval and turned a
        long-horizon reviewer into a per-batch rubber stamp. Short-horizon "was the task
        I just dispatched delivered?" is already answered by the per-worker task audit
        that the orchestrator runs inside the same harvest.
        """
        checkpoint_ids: list[str] = []
        for payload in payloads:
            _recorded, checkpoint_id = self._record_outcome(payload)
            if checkpoint_id is not None:
                checkpoint_ids.append(checkpoint_id)
        return checkpoint_ids

    def wait_for_decisions(
        self,
        checkpoint_ids: list[str],
        *,
        timeout_seconds: float | None = 1200.0,
    ) -> list[dict[str, Any]]:
        """Wait for specific checkpoint decisions; curator remains asynchronous."""
        requested = [str(item) for item in checkpoint_ids if str(item).strip()]
        if not requested:
            return []
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
        with self._decision_ready:
            while True:
                decisions = [self._read_checkpoint_decision(checkpoint_id) for checkpoint_id in requested]
                if all(decision is not None for decision in decisions):
                    return [_compact_decision(decision) for decision in decisions if decision is not None]
                if self.stop_event is not None and self.stop_event.is_set():
                    return []
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return []
                    self._decision_ready.wait(timeout=min(0.1, remaining))
                else:
                    self._decision_ready.wait(timeout=0.1)

    def _record_outcome(self, payload: dict[str, Any]) -> tuple[bool, str | None]:
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            return False, None
        with self._lock:
            seen = set(self._state.get("recorded_worker_ids") or [])
            if worker_id in seen:
                return False, None
            sequence = int(self._state.get("outcome_count") or 0) + 1
            record = self._outcome_record(payload, sequence=sequence)
            self.layout.progress_audit_outcomes_path.parent.mkdir(parents=True, exist_ok=True)
            with self.layout.progress_audit_outcomes_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._state["outcome_count"] = sequence
            recorded = list(self._state.get("recorded_worker_ids") or [])
            recorded.append(worker_id)
            self._state["recorded_worker_ids"] = recorded[-2000:]
            self._state["updated_at"] = _now_iso()

            checkpoint_id: str | None = None
            last_scheduled = int(self._state.get("last_scheduled_outcome") or 0)
            targeted_verified = (
                str(record.get("status") or "") == "verified"
                and bool(str(record.get("difficulty_id") or "").strip())
            )
            if targeted_verified:
                self._submit_evidence_checkpoint_locked(
                    checkpoint_id=f"targeted-verified-{sequence:04d}",
                    previous_watermark=sequence - 1,
                    watermark=sequence,
                    trigger_reason="targeted_verified_evidence",
                )
            if sequence - last_scheduled >= self.outcomes_per_audit:
                task = self._create_checkpoint_locked(
                    watermark=sequence,
                    previous_watermark=last_scheduled,
                    trigger_reason="periodic",
                )
                checkpoint_id = task.checkpoint_id
                self._state["last_scheduled_outcome"] = sequence
                pending = list(self._state.get("pending_checkpoints") or [])
                pending.append(checkpoint_id)
                self._state["pending_checkpoints"] = pending
                self._queue.put(task)
            self._save_state_locked()
        return True, checkpoint_id

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            latest = dict(self._state.get("latest") or {})
            completed = int(self._state.get("outcome_count") or 0)
            scheduled = int(self._state.get("last_scheduled_outcome") or 0)
            pending = list(self._state.get("pending_checkpoints") or [])
        return {
            "outcomes_recorded": completed,
            "outcomes_per_audit": self.outcomes_per_audit,
            "outcomes_until_next_audit": max(0, self.outcomes_per_audit - (completed - scheduled)),
            "pending_checkpoints": pending,
            "latest": latest or None,
        }

    def _restore_pending_task(self, checkpoint_id: str) -> ProgressAuditTask | None:
        checkpoint_dir = self.layout.progress_audits_dir / checkpoint_id
        manifest_path = checkpoint_dir / "manifest.json"
        evidence_path = checkpoint_dir / "evidence.md"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            watermark = int(manifest["watermark"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if not evidence_path.is_file():
            return None
        return ProgressAuditTask(
            checkpoint_id=checkpoint_id,
            watermark=watermark,
            previous_watermark=int(manifest.get("previous_watermark") or 0),
            evidence_path=evidence_path,
            checkpoint_dir=checkpoint_dir,
        )

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            self._run_task(task)

    def _run_task(self, task: ProgressAuditTask) -> None:
        status = "completed"
        audit_text = ""
        try:
            prompt = _audit_prompt(task, self.layout.workspace_dir)
            audit_text = self.audit_runner(task.evidence_path, prompt) if self.audit_runner else self._run_auditor(prompt)
            verdict, terminal_gap, repeated_blocker, route_contract_signals = _parse_audit(audit_text)
            if verdict is None:
                status = "invalid_audit"
            decision = {
                "checkpoint_id": task.checkpoint_id,
                "watermark": task.watermark,
                "status": status,
                "verdict": verdict,
                "terminal_gap": terminal_gap,
                "repeated_avoided_obligation": repeated_blocker,
                "route_contract_signals": route_contract_signals,
                "evidence_path": _relative_to_workspace(task.evidence_path, self.layout.workspace_dir),
                "audit_path": _relative_to_workspace(task.checkpoint_dir / "audit.md", self.layout.workspace_dir),
                "created_at": _now_iso(),
            }
        except Exception as exc:
            status = "failed"
            audit_text = f"# Progress Audit Failure\n\n{type(exc).__name__}: {exc}\n"
            decision = {
                "checkpoint_id": task.checkpoint_id,
                "watermark": task.watermark,
                "status": status,
                "verdict": None,
                "terminal_gap": "",
                "repeated_avoided_obligation": {},
                "route_contract_signals": "",
                "error": str(exc),
                "evidence_path": _relative_to_workspace(task.evidence_path, self.layout.workspace_dir),
                "audit_path": _relative_to_workspace(task.checkpoint_dir / "audit.md", self.layout.workspace_dir),
                "created_at": _now_iso(),
            }

        audit_path = task.checkpoint_dir / "audit.md"
        audit_path.write_text(audit_text.strip() + "\n", encoding="utf-8")
        decision = self._finish_task(task, decision)
        try:
            # 投影会读取刚写入的状态文件；与下一次状态替换共用锁，避免 Windows
            # 在读取句柄仍打开时拒绝原子替换。
            with self._lock:
                write_research_frontier_state(self.layout.workspace_dir)
        except OSError:
            pass
        curator_brief_path = _write_curator_brief(
            layout=self.layout,
            task=task,
            decision=decision,
            audit_path=audit_path,
        )

        if self.curator_queue is not None and status in {"completed", "invalid_audit"}:
            from .curator import CuratorTask

            self.curator_queue.submit(
                CuratorTask(
                    trace_segment=[],
                    source_label=f"portfolio-checkpoint/{task.checkpoint_id}",
                    task_kind="portfolio_checkpoint",
                    artifact_path=curator_brief_path,
                    audit_path=audit_path,
                )
            )
            # 审计报告本身也要沉淀成可复用知识：DAG curation 只改结构，不写失败模式。
            # 该任务派在结构 curation 之后，因此摘要可以反映刚刚归档的图状态。
            self.curator_queue.submit(
                CuratorTask(
                    trace_segment=[],
                    source_label=f"progress-audit/{task.checkpoint_id}",
                    task_kind="progress_audit",
                    audit_path=audit_path,
                )
            )

    def _finish_task(self, task: ProgressAuditTask, decision: dict[str, Any]) -> dict[str, Any]:
        with self._decision_ready:
            self._state["pending_checkpoints"] = [
                item for item in self._state.get("pending_checkpoints") or []
                if item != task.checkpoint_id
            ]
            previous_latest = dict(self._state.get("latest") or {})
            streak = _compute_stagnation_streak(previous_latest, decision)
            decision["stagnation_streak"] = streak
            decision["stagnation_level"] = _stagnation_level(streak, self.stagnation_force_pivot_streak)
            self._state["latest"] = decision
            self._state["updated_at"] = _now_iso()
            _write_json(task.checkpoint_dir / "decision.json", decision)
            self._save_state_locked()
            self._decision_ready.notify_all()
            return decision

    def _read_checkpoint_decision(self, checkpoint_id: str) -> dict[str, Any] | None:
        path = self.layout.progress_audits_dir / checkpoint_id / "decision.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _run_auditor(self, prompt: str) -> str:
        """Run the process auditor as an independent system role.

        This role is deliberately not registered as a subagent and has no delegation
        path to `research_reviewer`; it observes the worker portfolio from its own
        immutable evidence snapshot.
        """
        config = self.suite.agents.get("process_auditor")
        if config is None:
            raise RuntimeError("process_auditor agent config is required for progress audits")
        access = RoleWorkspaceAccess.process_auditor(Workspace(self.layout.workspace_dir))
        agent = Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=build_solver_tool_registry(access),
            stop_event=self.stop_event,
        )
        try:
            return agent.run(prompt, description="Periodic strategic progress audit").final_answer
        finally:
            agent.close()

    def _create_checkpoint_locked(
        self,
        *,
        watermark: int,
        previous_watermark: int,
        checkpoint_id: str | None = None,
        trigger_reason: str = "periodic",
    ) -> ProgressAuditTask:
        checkpoint_id = checkpoint_id or f"checkpoint-{watermark:04d}"
        checkpoint_dir = self.layout.progress_audits_dir / checkpoint_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        outcomes = _read_jsonl(self.layout.progress_audit_outcomes_path)
        evidence_path = checkpoint_dir / "evidence.md"
        evidence_path.write_text(
            _render_evidence(
                layout=self.layout,
                checkpoint_id=checkpoint_id,
                watermark=watermark,
                previous_watermark=previous_watermark,
                outcomes=outcomes,
            ),
            encoding="utf-8",
        )
        manifest = {
            "checkpoint_id": checkpoint_id,
            "watermark": watermark,
            "previous_watermark": previous_watermark,
            "outcomes_per_audit": self.outcomes_per_audit,
            "trigger_reason": trigger_reason,
            "created_at": _now_iso(),
            "evidence_path": _relative_to_workspace(evidence_path, self.layout.workspace_dir),
        }
        _write_json(checkpoint_dir / "manifest.json", manifest)
        (checkpoint_dir / "manifest.md").write_text(_render_manifest(manifest), encoding="utf-8")
        return ProgressAuditTask(
            checkpoint_id=checkpoint_id,
            watermark=watermark,
            previous_watermark=previous_watermark,
            evidence_path=evidence_path,
            checkpoint_dir=checkpoint_dir,
        )

    def _outcome_record(self, payload: dict[str, Any], *, sequence: int) -> dict[str, Any]:
        worker_dir = _safe_path(payload.get("worker_dir"))
        difficulty = _read_text(_safe_path(payload.get("difficulty_declaration_file")), limit=4000)
        review = _read_text(_safe_path(payload.get("review_file")), limit=4000)
        handoff = payload.get("difficulty_handoff")
        if not isinstance(handoff, dict):
            handoff = _read_json_object(_safe_path(payload.get("difficulty_handoff_file")))
        # 短程交付验收的结论要随不可变 outcome 一起留存：没有它，节点上的多次尝试
        # 只剩状态与次数，无法回答"为什么失败"。
        task_audit = payload.get("task_audit") if isinstance(payload.get("task_audit"), dict) else {}
        return {
            "sequence": sequence,
            "recorded_at": _now_iso(),
            "worker_id": str(payload.get("worker_id") or ""),
            "status": str(payload.get("status") or "unknown"),
            "failure_kind": str(payload.get("failure_kind") or ""),
            "delivery": str(task_audit.get("delivery") or ""),
            # task auditor 对本条交付相对其分派 milestone 的事实判定（achieved / contradicted /
            # inconclusive / not_reached）。它是"打回" plan 的运行时依据，必须随不可变 outcome
            # 一起留存，否则事后无法追溯某次冻结究竟因为哪个具体判定触发。
            "milestone_disposition": str(task_audit.get("milestone_disposition") or ""),
            "rejection_locus": str(task_audit.get("rejection_locus") or ""),
            "retry_assessment": str(task_audit.get("retry_assessment") or "")[:2000],
            "salvageable_content": str(task_audit.get("salvageable_content") or "")[:2000],
            "residual_obligation": str(task_audit.get("residual_obligation") or "")[:2000],
            "difficulty_id": payload.get("difficulty_id"),
            "method_id": payload.get("method_id"),
            # 路线标签与推荐类型：前者是"同一条数学路线"的唯一可靠键（method_id 只是
            # 证明体裁），后者区分图外探索与节点内尝试。两者都由调度侧提供。
            "route_label": str(payload.get("route_label") or ""),
            "reviewer_step_kind": str(payload.get("reviewer_step_kind") or ""),
            # 计划归因把 reviewer 的战略选择与 worker 实际产出连起来；它是审计事实，
            # 不赋予 process auditor 或 curator 任何调度/图写入权。
            "research_plan_id": str(payload.get("research_plan_id") or ""),
            "research_track_id": str(payload.get("research_track_id") or ""),
            "research_milestone_id": str(payload.get("research_milestone_id") or ""),
            "track_priority": str(payload.get("track_priority") or ""),
            "selection_scope": str(payload.get("selection_scope") or ""),
            "tabu_rule_ids": [str(item) for item in payload.get("tabu_rule_ids") or [] if str(item).strip()],
            "pinned_target": str(payload.get("pinned_target") or "")[:2000],
            "worker_hint": str(payload.get("worker_hint") or "")[:2000],
            "orchestrator_session_id": payload.get("orchestrator_session_id"),
            "rubric": str(payload.get("rubric") or "")[:4000],
            "summary": str(payload.get("summary") or "")[:4000],
            "difficulty_declaration": difficulty,
            "difficulty_handoff_file": str(payload.get("difficulty_handoff_file") or ""),
            "difficulty_handoff": handoff if isinstance(handoff, dict) else None,
            "review_excerpt": review,
            "verified_file": str(payload.get("verified_file") or ""),
            "theorem_check_file": str(payload.get("theorem_check_file") or ""),
            "proposition_file": str(payload.get("proposition_file") or ""),
            "worker_dir": str(worker_dir or ""),
            "solved_problem": bool(payload.get("solved_problem")),
            "is_consolidation": bool(payload.get("is_consolidation")),
            "target_achieved": payload.get("target_achieved"),
        }

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.layout.progress_audit_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return {
            "outcome_count": int(data.get("outcome_count") or 0),
            "last_scheduled_outcome": int(data.get("last_scheduled_outcome") or 0),
            "recorded_worker_ids": list(data.get("recorded_worker_ids") or []),
            "pending_checkpoints": list(data.get("pending_checkpoints") or []),
            "latest": data.get("latest") if isinstance(data.get("latest"), dict) else {},
            "updated_at": str(data.get("updated_at") or ""),
        }

    def _save_state_locked(self) -> None:
        _write_json(self.layout.progress_audit_state_path, self._state)


def plan_outcome_assessments(
    workspace_dir: Path,
    outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Derive each proposal's post-execution audit verdicts from immutable records.

    The outcome ledger and the per-checkpoint ``decision.json`` files are already the
    authoritative records. Deriving this mapping on read keeps the research-plan files
    single-writer: only the orchestrator writes them, so a background audit can never
    race with a plan update or lose one.
    """
    if outcomes is None:
        outcomes = _read_jsonl(workspace_dir / "progress_audit_outcomes.jsonl")
    checkpoints: list[tuple[int, int, dict[str, Any]]] = []
    audits_dir = workspace_dir / "progress_audits"
    try:
        directories = sorted(path for path in audits_dir.glob("checkpoint-*") if path.is_dir())
    except OSError:
        directories = []
    for directory in directories:
        decision = _read_json_object(directory / "decision.json")
        if decision is None:
            continue
        watermark = int(decision.get("watermark") or 0)
        manifest = _read_json_object(directory / "manifest.json") or {}
        previous = int(manifest.get("previous_watermark") or 0)
        checkpoints.append((watermark, previous, decision))
    checkpoints.sort(key=lambda item: item[0])

    plan_sequences: dict[str, list[int]] = {}
    for item in outcomes:
        plan_id = str(item.get("research_plan_id") or "").strip()
        if plan_id:
            plan_sequences.setdefault(plan_id, []).append(int(item.get("sequence") or 0))

    assessments: dict[str, list[dict[str, Any]]] = {}
    for watermark, previous, decision in checkpoints:
        compact = {
            "checkpoint_id": str(decision.get("checkpoint_id") or ""),
            "watermark": watermark,
            "verdict": decision.get("verdict"),
            "terminal_gap": str(decision.get("terminal_gap") or "")[:1200],
            "route_contract_signals": str(decision.get("route_contract_signals") or "")[:1200],
            "audit_path": str(decision.get("audit_path") or ""),
            "created_at": str(decision.get("created_at") or ""),
        }
        for plan_id, sequences in plan_sequences.items():
            if any(previous < sequence <= watermark for sequence in sequences):
                assessments.setdefault(plan_id, []).append(compact)
    return assessments


def _render_evidence(
    *,
    layout: "ProjectLayout",
    checkpoint_id: str,
    watermark: int,
    previous_watermark: int,
    outcomes: list[dict[str, Any]],
) -> str:
    visible = [item for item in outcomes if int(item.get("sequence") or 0) <= watermark]
    delta = [item for item in visible if int(item.get("sequence") or 0) > previous_watermark]
    counts: dict[str, int] = {}
    for item in visible:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    lines = [
        f"# Progress Audit Evidence: {checkpoint_id}",
        "",
        "This immutable snapshot was assembled by runtime code. It contains both verified and failed worker outcomes. "
        "Only cited files under `verified_propositions/` count as established mathematical facts.",
        "",
        "## Snapshot",
        f"- Outcome watermark: {watermark}",
        f"- Outcomes since previous checkpoint: {previous_watermark + 1}-{watermark}",
        f"- Cumulative status counts: {json.dumps(counts, ensure_ascii=False, sort_keys=True)}",
        "",
        "## Original Objective",
        _read_text(layout.workspace_dir / "problem.md", limit=12000) or "(problem.md unavailable)",
        "",
        "## Current Curated Difficulty Frontier",
        _read_text(layout.workspace_dir / "curation_records" / "difficulty_dag.json", limit=12000) or "(difficulty DAG unavailable)",
        "",
        "## Global Research Plan Execution History",
        "This runtime-generated section joins every persisted research plan to immutable settled outcomes. "
        "It is execution evidence: the process auditor judges progress, while the research reviewer chooses the next route.",
        *_render_research_plan_execution_history(layout.workspace_dir, visible),
        "",
        "## Newly Settled Outcomes",
    ]
    lines.extend(_render_outcomes(delta, layout.workspace_dir))
    lines.extend(["", "## Cumulative Outcome Ledger (Index)", *_render_ledger_summary(visible, layout.workspace_dir)])
    return "\n".join(lines).rstrip() + "\n"


def research_plan_execution_summary(
    workspace_dir: Path,
    outcomes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return a complete, evidence-only view of every persisted research plan.

    Plans name intended tracks; immutable outcomes name actual worker results. Joining them
    here gives the process auditor and later reviewer a project-wide strategy history
    without treating plan prose as mathematical proof.
    """
    if outcomes is None:
        outcomes = _read_jsonl(workspace_dir / "progress_audit_outcomes.jsonl")
    derived_assessments = plan_outcome_assessments(workspace_dir, outcomes)
    plans_dir = workspace_dir / "curation_records" / "research_plans"
    try:
        # 计划历史是 reviewer 的长期策略记忆，不能只保留最近窗口；原始 outcome
        # 已在不可变 ledger 中按路径可核查，这里保留所有 plan 的压缩执行事实。
        plan_paths = sorted(
            (path for path in plans_dir.glob("plan-*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        plan_paths = []
    plans: dict[str, dict[str, Any]] = {}
    for path in plan_paths:
        value = _read_json_object(path)
        plan_id = str((value or {}).get("plan_id") or path.stem).strip()
        if not plan_id:
            continue
        plans[plan_id] = {
            "plan_id": plan_id,
            "plan_path": _relative_to_workspace(path, workspace_dir),
            "execution_status": str((value or {}).get("execution_status") or "planned"),
            "selected_track_ids": [str(item) for item in (value or {}).get("selected_track_ids") or [] if str(item).strip()],
            "spawned_worker_ids": [str(item) for item in (value or {}).get("spawned_worker_ids") or [] if str(item).strip()],
            "pending_track_ids": [str(item) for item in (value or {}).get("pending_track_ids") or [] if str(item).strip()],
            "hold_reason": str((value or {}).get("hold_reason") or "")[:2000],
            "parent_proposal_id": str((value or {}).get("parent_proposal_id") or ""),
            "evidence_watermark": int((value or {}).get("evidence_watermark") or 0),
            "outcome_assessments": derived_assessments.get(plan_id, []),
            "objective": "",
            "strategy": "",
            "tracks": {},
        }
        recommendation = (value or {}).get("recommendation")
        research_plan = recommendation.get("research_plan") if isinstance(recommendation, dict) else None
        if not isinstance(research_plan, dict):
            continue
        plans[plan_id]["objective"] = str(research_plan.get("objective") or "")[:2000]
        plans[plan_id]["strategy"] = str(research_plan.get("strategy") or "")[:3000]
        for track in research_plan.get("tracks") or []:
            if not isinstance(track, dict):
                continue
            track_id = str(track.get("track_id") or "").strip()
            if not track_id:
                continue
            plans[plan_id]["tracks"][track_id] = {
                "track_id": track_id,
                "priority": str(track.get("priority") or ""),
                "kind": str(track.get("kind") or ""),
                "selection_scope": str(track.get("selection_scope") or ""),
                "route_label": str(track.get("route_label") or ""),
                "tabu_rule_ids": [str(item) for item in track.get("tabu_rule_ids") or [] if str(item).strip()],
                "research_goal": str(track.get("research_goal") or "")[:2000],
                "route_contract": dict(track.get("route_contract") or {}) if isinstance(track.get("route_contract"), dict) else {},
                "milestones": [dict(item) for item in track.get("milestones") or [] if isinstance(item, dict)][:4],
                "avoid": str(track.get("avoid") or "")[:2000],
                "outcomes": [],
            }
    for item in outcomes:
        plan_id = str(item.get("research_plan_id") or "").strip()
        track_id = str(item.get("research_track_id") or "").strip()
        if not plan_id or plan_id not in plans:
            continue
        track = plans[plan_id]["tracks"].get(track_id)
        if track is None:
            track = plans[plan_id]["tracks"].setdefault(track_id or "unmapped", {
                "track_id": track_id or "unmapped",
                "priority": str(item.get("track_priority") or ""),
                "kind": str(item.get("reviewer_step_kind") or ""),
                "route_label": str(item.get("route_label") or ""),
                "research_goal": str(item.get("pinned_target") or "")[:2000],
                "route_contract": {},
                "milestones": [],
                "avoid": "",
                "outcomes": [],
            })
        track["outcomes"].append({
            "sequence": int(item.get("sequence") or 0),
            "worker_id": str(item.get("worker_id") or ""),
            "status": str(item.get("status") or "unknown"),
            "delivery": str(item.get("delivery") or "unknown"),
            "milestone_disposition": str(item.get("milestone_disposition") or ""),
            "failure_kind": str(item.get("failure_kind") or ""),
            "selection_scope": str(item.get("selection_scope") or ""),
            "research_milestone_id": str(item.get("research_milestone_id") or ""),
            "tabu_rule_ids": [str(value) for value in item.get("tabu_rule_ids") or [] if str(value).strip()],
            "residual_obligation": str(item.get("residual_obligation") or "")[:1200],
            "verified_proposition_ref": _display_path(str(item.get("verified_file") or ""), workspace_dir),
            "task_audit_ref": f"task_audits/{str(item.get('worker_id') or '')}.json" if str(item.get("worker_id") or "") else "",
            "local_difficulty_ref": _display_path(str(item.get("difficulty_handoff_file") or ""), workspace_dir),
        })
    rendered: list[dict[str, Any]] = []
    for plan in plans.values():
        tracks = []
        for track in plan["tracks"].values():
            track["outcomes"].sort(key=lambda item: item["sequence"])
            tracks.append({
                **{key: value for key, value in track.items() if key != "outcomes"},
                # 每条 route 的全部 settled outcome 都是下一次策略反思的事实；不要按
                # 最近窗口丢弃早期的反例、verified bridge 或重复 blocker。
                "outcomes": track["outcomes"],
            })
        rendered.append({**{key: value for key, value in plan.items() if key != "tracks"}, "tracks": tracks})
    return rendered


def _render_research_plan_execution_history(workspace_dir: Path, outcomes: list[dict[str, Any]]) -> list[str]:
    plans = research_plan_execution_summary(workspace_dir, outcomes)
    if not plans:
        return ["- No persisted research plans are available yet."]
    lines: list[str] = []
    for plan in plans:
        lines.extend([
            f"### Plan `{plan['plan_id']}`",
            f"- Plan record: `{plan['plan_path']}`",
            f"- Execution status: `{plan['execution_status']}`; selected tracks: {', '.join(plan['selected_track_ids']) or 'none'}; spawned workers: {', '.join(plan['spawned_worker_ids']) or 'none'}",
            f"- Pending tracks: {', '.join(plan['pending_track_ids']) or 'none'}",
            f"- HOLD reason: {plan['hold_reason'] or 'none'}",
            f"- Parent proposal: `{plan['parent_proposal_id'] or 'none'}`; evidence watermark: `{plan['evidence_watermark'] or '-'}`",
            f"- Outcome audits: {', '.join(str(item.get('verdict') or 'unknown') for item in plan['outcome_assessments']) or 'none recorded'}",
            f"- Objective: {plan['objective'] or 'not recorded'}",
            f"- Strategy: {plan['strategy'] or 'not recorded'}",
        ])
        for track in plan["tracks"]:
            lines.extend([
                f"#### Track `{track['track_id']}` ({track['priority'] or 'unspecified'})",
                f"- Route: `{track['route_label'] or 'unspecified'}`; kind: `{track['kind'] or 'unspecified'}`",
                f"- Research goal: {track['research_goal'] or 'not recorded'}",
                f"- Route contract: hypothesis={str(track.get('route_contract', {}).get('hypothesis') or 'not recorded')[:1200]}; success={str(track.get('route_contract', {}).get('success_condition') or 'not recorded')[:1200]}; failure={str(track.get('route_contract', {}).get('failure_condition') or 'not recorded')[:1200]}; falsification={str(track.get('route_contract', {}).get('falsification_condition') or 'not recorded')[:1200]}",
                f"- Milestones: {', '.join(str(item.get('milestone_id') or '') for item in track.get('milestones') or [] if isinstance(item, dict)) or 'not recorded'}",
                f"- Avoid: {track['avoid'] or 'none recorded'}",
            ])
            if not track["outcomes"]:
                lines.append("- Settled outcomes: none yet; do not treat this route as failed.")
                continue
            for outcome in track["outcomes"]:
                refs = [ref for ref in (outcome["verified_proposition_ref"], outcome["task_audit_ref"], outcome["local_difficulty_ref"]) if ref]
                lines.append(
                    f"- Outcome #{outcome['sequence']} worker `{outcome['worker_id']}` at milestone `{outcome.get('research_milestone_id') or '-'}`: status=`{outcome['status']}`, delivery=`{outcome['delivery']}`, "
                    f"milestone_disposition=`{outcome.get('milestone_disposition') or 'unknown'}`, "
                    f"failure=`{outcome['failure_kind'] or '-'}`; refs: {', '.join(f'`{ref}`' for ref in refs) or 'none'}"
                )
                if outcome["residual_obligation"]:
                    lines.append(f"  - Residual obligation: {outcome['residual_obligation']}")
        lines.append("")
    return lines


def _render_outcomes(
    outcomes: list[dict[str, Any]],
    workspace_dir: Path,
) -> list[str]:
    if not outcomes:
        return ["- None."]
    lines: list[str] = []
    for item in outcomes:
        lines.extend([
            f"### Outcome {item.get('sequence', '?')}: {item.get('status', 'unknown')}",
            f"- Difficulty / method: `{item.get('difficulty_id') or '(root candidate)'} / {item.get('method_id') or '-'}`",
            f"- Orchestrator session: `{item.get('orchestrator_session_id') or '-'}`",
            f"- Research plan / track / milestone: `{item.get('research_plan_id') or '-'}` / `{item.get('research_track_id') or '-'}` / `{item.get('research_milestone_id') or '-'}` ({item.get('track_priority') or 'unspecified'})",
            f"- Route / step kind: `{item.get('route_label') or '-'}` / `{item.get('reviewer_step_kind') or '-'}`",
            f"- Delivery: `{item.get('delivery') or 'unknown'}`; task audit: `task_audits/{item.get('worker_id') or '-'}.json`",
            f"- Milestone disposition: `{item.get('milestone_disposition') or 'unknown'}`"
            + (
                f" — rejection locus: `{item.get('rejection_locus')}`"
                if item.get("milestone_disposition") == "contradicted" and item.get("rejection_locus")
                else ""
            ),
            f"- Failure kind: `{item.get('failure_kind') or '-'}`",
            f"- Solves original problem: `{bool(item.get('solved_problem'))}`",
        ])
        handoff = item.get("difficulty_handoff")
        if isinstance(handoff, dict):
            lines.extend([
                "#### Worker-Reported Obstacle",
                f"- Obstacle: {str(handoff.get('obstacle') or 'not stated')[:1800]}",
                f"- Delivered instead of assigned target: {str(handoff.get('delivered_instead') or 'not stated')[:1800]}",
                f"- Obstacle scope: `{handoff.get('obstacle_scope') or 'unclear'}` (reported by `{handoff.get('obstacle_reporting') or 'worker_recorded'}`)",
                *[
                    f"- {str(record.get('role') or 'worker')}: {str(record.get('obstacle') or '')[:1200]}"
                    + (f" [task: {str(record.get('delegated_description') or '')[:400]}]" if record.get('delegated_description') else "")
                    for record in handoff.get("obstacle_records") or []
                    if isinstance(record, dict) and str(record.get("obstacle") or "").strip()
                ][-8:],
                f"- Evidence refs: {', '.join(str(ref) for ref in handoff.get('evidence_refs') or []) or 'none'}",
                "",
            ])
        for key, label, limit in (
            ("summary", "Runtime summary", 1800),
            ("difficulty_declaration", "Worker difficulty declaration", 2200),
            ("review_excerpt", "Verifier review excerpt", 1800),
        ):
            text = str(item.get(key) or "").strip()
            if text:
                lines.extend([f"#### {label}", text[:limit], ""])
        refs = []
        for field in ("verified_file", "theorem_check_file", "proposition_file"):
            raw = str(item.get(field) or "").strip()
            if not raw:
                continue
            refs.append(_display_path(raw, workspace_dir))
        if refs:
            lines.append("- Artifact paths: " + ", ".join(f"`{path}`" for path in refs))
        lines.append("")
    return lines


def _render_ledger_summary(outcomes: list[dict[str, Any]], workspace_dir: Path) -> list[str]:
    """Render the cumulative ledger as one compact row per outcome.

    The delta section already carries full evidence for newly settled work.  Repeating
    every historical summary, declaration, and review excerpt at each checkpoint makes
    audit cost grow quadratically in the number of outcomes, so history is kept as a
    scannable index whose cited artifacts can be read on demand.
    """
    if not outcomes:
        return ["- None."]
    lines = [
        "One row per settled outcome. Read a cited artifact only when the audit needs its exact content.",
        "",
        "| # | Status | Difficulty | Method | Session | Failure | Milestone disposition | Verified artifact |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in outcomes:
        verified = str(item.get("verified_file") or "").strip()
        verified_cell = f"`{_display_path(verified, workspace_dir)}`" if verified else "-"
        lines.append(
            "| {sequence} | {status} | `{difficulty}` | `{method}` | `{session}` | `{failure}` | `{disposition}` | {verified} |".format(
                sequence=item.get("sequence", "?"),
                status=item.get("status", "unknown"),
                difficulty=item.get("difficulty_id") or "(root candidate)",
                method=item.get("method_id") or "-",
                disposition=item.get("milestone_disposition") or "-",
                session=item.get("orchestrator_session_id") or "-",
                failure=item.get("failure_kind") or "-",
                verified=verified_cell,
            )
        )
    return lines


def _render_manifest(manifest: dict[str, Any]) -> str:
    return "\n".join([
        f"# Progress Audit {manifest['checkpoint_id']}",
        "",
        f"- Outcome watermark: {manifest['watermark']}",
        f"- Previous watermark: {manifest['previous_watermark']}",
        f"- Trigger interval: {manifest['outcomes_per_audit']} settled outcomes",
        f"- Trigger reason: `{manifest.get('trigger_reason') or 'periodic'}`",
        f"- Created at: {manifest['created_at']}",
        f"- Evidence: `{manifest['evidence_path']}`",
        "",
    ])


def _write_curation_input(*, layout: "ProjectLayout", task: ProgressAuditTask) -> Path:
    """Build the curator's structured, evidence-only DAG reconciliation view."""
    outcomes = _read_jsonl(layout.progress_audit_outcomes_path)
    delta = [
        item for item in outcomes
        if task.previous_watermark < int(item.get("sequence") or 0) <= task.watermark
    ]
    handoffs: list[dict[str, Any]] = []
    targeted_verified: list[dict[str, Any]] = []
    for item in delta:
        handoff = item.get("difficulty_handoff")
        if isinstance(handoff, dict) and str(handoff.get("obstacle") or "").strip():
            handoffs.append({
                "handoff_id": str(handoff.get("handoff_id") or "").strip(),
                "assigned_difficulty_id": str(item.get("difficulty_id") or "").strip() or None,
                "assigned_target": str(handoff.get("assigned_target") or item.get("pinned_target") or "")[:4000],
                "method_id": str(item.get("method_id") or ""),
                "execution_status": str(item.get("status") or ""),
                # task auditor 的事实判定：一个 contradicted milestone 是 curator 判断
                # status_updates 时的直接引用依据（见 curator.md 规则3），不是仅由 obstacle 报告推断。
                "milestone_disposition": str(item.get("milestone_disposition") or ""),
                "obstacle": str(handoff.get("obstacle") or "")[:4000],
                "delivered_instead": str(handoff.get("delivered_instead") or "")[:4000],
                "obstacle_scope": str(handoff.get("obstacle_scope") or "unclear"),
                "obstacle_reporting": str(handoff.get("obstacle_reporting") or "worker_recorded"),
                "obstacle_records": [
                    {
                        "role": str(record.get("role") or ""),
                        "obstacle": str(record.get("obstacle") or "")[:4000],
                        "delivered_instead": str(record.get("delivered_instead") or "")[:4000],
                        "obstacle_scope": str(record.get("obstacle_scope") or ""),
                        "delegated_description": str(record.get("delegated_description") or "")[:2000],
                        "delegated_task": str(record.get("delegated_task") or "")[:4000],
                        "subagent_session_id": str(record.get("subagent_session_id") or "")[:300],
                    }
                    for record in handoff.get("obstacle_records") or []
                    if isinstance(record, dict) and str(record.get("obstacle") or "").strip()
                ][-16:],
                "evidence_refs": [str(value) for value in handoff.get("evidence_refs") or [] if str(value).strip()],
            })
        if str(item.get("status") or "") == "verified" and str(item.get("difficulty_id") or "").strip():
            targeted_verified.append({
                "assigned_difficulty_id": str(item.get("difficulty_id") or ""),
                "method_id": str(item.get("method_id") or ""),
                "summary": str(item.get("summary") or "")[:4000],
                "verified_file": str(item.get("verified_file") or ""),
                "theorem_check_file": str(item.get("theorem_check_file") or ""),
                "review_file": str(item.get("review_file") or ""),
            })
    path = task.checkpoint_dir / "curation_input.json"
    _write_json(path, {
        "schema_version": 1,
        "checkpoint_id": task.checkpoint_id,
        "outcome_range": [task.previous_watermark + 1, task.watermark],
        "canonical_dag_path": "curation_records/difficulty_dag.json",
        "worker_obstacles": handoffs,
        "targeted_verified_results": targeted_verified,
        "instructions": (
            "Read canonical_dag_path and cited artifacts before curation. Handoffs are local obstacle reports; "
            "only create or merge nodes when evidence supports a checkable obligation. A targeted verified result may "
            "support status=refuted only when it directly contradicts the assigned canonical node."
        ),
    })
    return path


def _write_evidence_checkpoint_brief(
    *,
    layout: "ProjectLayout",
    checkpoint_id: str,
    curation_input_path: Path,
    trigger_reason: str,
) -> Path:
    """Write the minimal curator brief for direct verified-evidence curation."""
    path = curation_input_path.with_name("curator_brief.md")
    path.write_text("\n".join([
        f"# Targeted Verified Evidence Checkpoint: {checkpoint_id}",
        "",
        "This checkpoint exists for direct canonical-DAG curation after a verified result. It is not a process audit and has no audit verdict.",
        "",
        f"- Trigger: `{trigger_reason}`",
        "- Read `curation_input.json`, `curation_records/difficulty_dag.json`, and cited verified artifacts.",
        "- Decide whether the verified result advances, resolves, or directly refutes its assigned canonical difficulty.",
        "- Call `CurateDifficultyDag` exactly once; do not request or wait for a process audit.",
        "",
    ]), encoding="utf-8")
    return path


def _write_curator_brief(
    *,
    layout: "ProjectLayout",
    task: ProgressAuditTask,
    decision: dict[str, Any],
    audit_path: Path,
) -> Path:
    """Materialize a compact, cross-checkpoint comparison brief for the curator."""
    previous = _previous_checkpoint_decision(layout.progress_audits_dir, before_watermark=task.watermark)
    events = read_recent_events(layout, limit=30)
    event_lines = _render_curation_events(events)
    reviewer_observations = _recent_reviewer_observations(layout.workspace_dir)
    curation_input_path = _write_curation_input(layout=layout, task=task)
    brief_path = task.checkpoint_dir / "curator_brief.md"
    lines = [
        f"# Portfolio Curation Brief: {task.checkpoint_id}",
        "",
        "This brief is operational evidence for curation. It is not itself a mathematical proof.",
        "",
        "## Current Checkpoint Artifacts",
        f"- Evidence: `{relative_path(task.evidence_path, layout.workspace_dir)}`",
        f"- Process audit: `{relative_path(audit_path, layout.workspace_dir)}`",
        f"- Machine decision: `{relative_path(task.checkpoint_dir / 'decision.json', layout.workspace_dir)}`",
        f"- Structured DAG curation input: `{relative_path(curation_input_path, layout.workspace_dir)}`",
        "",
        "## Reviewer Graph Observations",
        *(
            [
                "These are candidate graph corrections, not commands. Verify the cited evidence before changing a canonical node, edge, or status.",
                "```json",
                json.dumps(reviewer_observations, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
            ]
            if reviewer_observations else ["- None recorded since the recent planning cycle."]
        ),
        "",
        "## Current Process Decision",
        f"- Verdict: `{decision.get('verdict') or 'unknown'}`",
        f"- Terminal gap: {decision.get('terminal_gap') or 'not stated'}",
        "",
        "## Route Contract Signals (per-milestone factual judgments)",
        "These are factual `supports`/`contradicts`/`inconclusive`/`off_scope` signals about specific plan milestones, "
        "not a route selection. Use them only as cited evidence for archiving attempts or, where a signal directly "
        "supports or contradicts an existing canonical node, for a cited `status_updates` entry.",
        decision.get("route_contract_signals") or "- None recorded for this checkpoint.",
        "",
        "## Repeated Avoided Obligation",
        _render_blocker_brief(decision.get("repeated_avoided_obligation")),
        "",
        "## Comparison With Previous Checkpoint",
    ]
    if previous:
        lines.extend([
            f"- Previous checkpoint: `{previous.get('checkpoint_id')}`",
            f"- Previous verdict: `{previous.get('verdict') or 'unknown'}`",
            f"- Previous terminal gap: {previous.get('terminal_gap') or 'not stated'}",
            "- Compare whether the terminal gap, outcome classifications, and avoided obligations changed substantively rather than cosmetically.",
        ])
    else:
        lines.append("- No earlier completed checkpoint is available; establish the first comparison baseline.")
    lines.extend([
        "",
        "## Recent Orchestrator Session Facts",
        *event_lines,
        "",
        "## Required Comparison Questions",
        "- Which attempts share the same target/gap but differ in method, summary, or verification outcome?",
        "- Which correct propositions were incidental because the audit could not connect them to the terminal gap?",
        "- Which avoided obligation repeats across multiple outcomes?",
        "- Which observed pattern is reusable enough to record as a strategy rule, and what evidence or exception bounds that rule?",
        "",
        "Do not copy internal worker/session identifiers into knowledge files. Use the raw artifacts only to derive de-identified, evidence-bounded lessons.",
    ])
    brief_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return brief_path


def _recent_reviewer_observations(workspace_dir: Path) -> list[dict[str, Any]]:
    plans_dir = Path(workspace_dir) / "curation_records" / "research_plans"
    try:
        plan_paths = sorted(
            (path for path in plans_dir.glob("plan-*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:20]
    except OSError:
        return []
    observations: list[dict[str, Any]] = []
    for path in plan_paths:
        value = _read_json_object(path)
        recommendation = value.get("recommendation") if isinstance(value, dict) else None
        for observation in recommendation.get("graph_observations") or [] if isinstance(recommendation, dict) else []:
            if isinstance(observation, dict):
                observations.append({
                    "plan_id": str(value.get("plan_id") or path.stem),
                    "plan_path": _relative_to_workspace(path, workspace_dir),
                    **observation,
                })
    return observations[:24]


def _previous_checkpoint_decision(progress_audits_dir: Path, *, before_watermark: int) -> dict[str, Any] | None:
    candidates: list[tuple[int, Path]] = []
    for directory in progress_audits_dir.glob("checkpoint-*"):
        decision_path = directory / "decision.json"
        try:
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            watermark = int(decision.get("watermark") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if watermark < before_watermark:
            candidates.append((watermark, decision_path))
    if not candidates:
        return None
    _watermark, path = max(candidates, key=lambda item: item[0])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _render_curation_events(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["- No orchestrator session facts recorded yet."]
    lines: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "unknown")
        if kind == "orchestrator_session_started":
            lines.append("- A new orchestrator session began.")
        elif kind == "orchestrator_session_finished":
            lines.append(
                "- An orchestrator session ended: "
                f"solved={bool(event.get('solved'))}, worker_results={event.get('worker_results', 0)}."
            )
        elif kind == "local_difficulty_followup_started":
            lines.append(
                "- A bounded local follow-up was dispatched from a recorded worker obstacle "
                f"(method={event.get('method_id') or 'unknown'})."
            )
    return lines or ["- No relevant orchestrator transition facts in the retained window."]


def _audit_prompt(task: ProgressAuditTask, workspace_dir: Path) -> str:
    evidence_path = _relative_to_workspace(task.evidence_path, workspace_dir)
    return (
        "Run a periodic strategic progress audit. Read the immutable evidence snapshot at "
        f"`{evidence_path}` first, then inspect cited verified propositions or knowledge notes only when needed. "
        "Do not rely on unverified drafts as established evidence. This is an audit of whether the completed worker portfolio "
        "is moving toward `problem.md`, not a request to solve the problem yourself.\n\n"
        "Your output MUST start with these exact sections:\n"
        "### Progress Verdict\n"
        "VERDICT: ADVANCING | STALLED | MISALIGNED | INSUFFICIENT_EVIDENCE\n"
        "### Current Best Verified Position\n"
        "### Terminal Gap\n"
        "### Outcome Classification\n"
        "### Repeated Avoided Obligation\n"
        "### Route Contract Signals\n"
        "### Cited Evidence\n\n"
        "Classify every new outcome as direct_advance, supporting, incidental, duplicate, or failed. "
        "A mathematically correct result is incidental unless you can cite how it closes a named terminal gap. "
        "A separate per-worker task audit already judged whether each dispatch was delivered; do not re-check acceptance "
        "criteria here. Your question is whether the portfolio advances `problem.md`. For every newly evidenced plan milestone, record a "
        "factual `supports`, `contradicts`, `inconclusive`, or `off_scope` route-contract signal with evidence; do not select a replacement "
        "route. Report only the evidence, terminal gap, and repeated failure mechanisms; the research reviewer chooses every next target and method family.\n\n"
        "If the repeated avoided obligation is concrete enough to give the curator a lead, append these exact candidate lines "
        "after the cited evidence (otherwise write NONE for both):\n"
        "BLOCKER_SOURCE_DIFFICULTY_ID: worker-local-source-id | NONE\n"
        "BLOCKER_STATEMENT: exact unresolved mathematical obligation | NONE\n"
        "The auditor does not assign canonical identity, parent edges, or dispatch work. The curator reconciles evidence "
        "into the persistent difficulty DAG at the checkpoint."
    )


_STAGNATION_VERDICTS = {"STALLED", "MISALIGNED"}


def _normalize_blocker(value: Any) -> str:
    """Return a comparable key for a repeated_avoided_obligation payload.

    Comparison is deliberately coarse (lower-cased statement text, whitespace
    collapsed) rather than requiring a stable canonical ID: the auditor writes free
    text, and small rewordings of the same obligation must still count as the same
    streak. An empty/missing statement never matches another empty statement, so the
    absence of a blocker never silently counts as "same blocker across cycles".
    """
    if not isinstance(value, dict):
        return ""
    statement = " ".join(str(value.get("statement") or "").lower().split())
    return statement


def _compute_stagnation_streak(previous: dict[str, Any], decision: dict[str, Any]) -> int:
    """Count consecutive checkpoints citing the same blocker under a stalled verdict.

    This is pure runtime bookkeeping over immutable checkpoint decisions -- it does not
    ask an LLM to remember or self-report a streak. Any verdict outside
    ``_STAGNATION_VERDICTS`` resets the streak to 0; a matching blocker under a stalled
    verdict increments the previous streak; a stalled verdict with a new or absent
    blocker restarts the streak at 1 so an unlabeled-but-real stall still accumulates.
    """
    if str(decision.get("verdict") or "").upper() not in _STAGNATION_VERDICTS:
        return 0
    current_blocker = _normalize_blocker(decision.get("repeated_avoided_obligation"))
    previous_blocker = _normalize_blocker(previous.get("repeated_avoided_obligation"))
    previous_streak = int(previous.get("stagnation_streak") or 0)
    if current_blocker and current_blocker == previous_blocker:
        return previous_streak + 1
    return 1


def _stagnation_level(streak: int, force_pivot_streak: int) -> str:
    """Map a consecutive-stall streak to a coarse escalation level.

    Thresholds are relative to the configured ``force_pivot_streak`` so operators can
    tune sensitivity (``stagnation_force_pivot_streak`` policy) without touching code:
    ``WATCH`` at half the threshold, ``ESCALATE`` one short of it, ``FORCE_PIVOT`` once
    the threshold is reached or exceeded.
    """
    if streak <= 0:
        return "NONE"
    if streak >= force_pivot_streak:
        return "FORCE_PIVOT"
    if streak >= max(2, force_pivot_streak - 1):
        return "ESCALATE"
    if streak >= max(1, force_pivot_streak // 2):
        return "WATCH"
    return "NONE"


def _compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Expose the bounded decision surface needed by the orchestrator."""
    return {
        "checkpoint_id": str(decision.get("checkpoint_id") or ""),
        "watermark": int(decision.get("watermark") or 0),
        "status": str(decision.get("status") or "unknown"),
        "verdict": decision.get("verdict"),
        "terminal_gap": str(decision.get("terminal_gap") or "")[:4000],
        "repeated_avoided_obligation": decision.get("repeated_avoided_obligation")
        if isinstance(decision.get("repeated_avoided_obligation"), dict) else {},
        "route_contract_signals": str(decision.get("route_contract_signals") or "")[:4000],
        "stagnation_streak": int(decision.get("stagnation_streak") or 0),
        "stagnation_level": str(decision.get("stagnation_level") or "NONE"),
        "evidence_path": str(decision.get("evidence_path") or ""),
        "audit_path": str(decision.get("audit_path") or ""),
    }


def _parse_audit(text: str) -> tuple[str | None, str, dict[str, Any], str]:
    verdict_match = re.search(r"(?mi)^\s*VERDICT:\s*(ADVANCING|STALLED|MISALIGNED|INSUFFICIENT_EVIDENCE)\s*$", text)
    verdict = verdict_match.group(1).upper() if verdict_match else None
    if verdict not in _VALID_VERDICTS:
        verdict = None
    return (
        verdict,
        _extract_section(text, "Terminal Gap"),
        _extract_repeated_blocker(text),
        _extract_section(text, "Route Contract Signals"),
    )


def _extract_repeated_blocker(text: str) -> dict[str, str]:
    source_match = re.search(r"(?mi)^\s*BLOCKER_SOURCE_DIFFICULTY_ID:\s*([A-Za-z0-9][A-Za-z0-9._-]*|NONE)\s*$", text)
    statement_match = re.search(r"(?mi)^\s*BLOCKER_STATEMENT:\s*(.+?)\s*$", text)
    if not all((source_match, statement_match)):
        return {}
    source_difficulty_id = source_match.group(1)
    statement = statement_match.group(1).strip()
    if source_difficulty_id == "NONE" or statement == "NONE":
        return {}
    return {"source_difficulty_id": source_difficulty_id, "statement": statement[:2000]}


def _render_blocker_brief(value: Any) -> str:
    if not isinstance(value, dict) or not value.get("statement"):
        return "no audit candidate recorded"
    return (
        f"Audit difficulty source `{value.get('source_difficulty_id')}` — "
        f"{value.get('statement')}\n\n"
        "The curator must independently decide canonical identity, parent relation, status, and evidence provenance."
    )


def _extract_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ims)^###\s+{re.escape(heading)}\s*$\s*(.*?)(?=^###\s+|\Z)",
        text,
    )
    return match.group(1).strip()[:4000] if match else ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _read_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_text(path: Path | None, *, limit: int) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:limit].strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _safe_path(raw: Any) -> Path | None:
    if not raw:
        return None
    try:
        return Path(str(raw))
    except (TypeError, ValueError):
        return None


def _display_path(raw: str, workspace_dir: Path) -> str:
    try:
        return _relative_to_workspace(Path(raw), workspace_dir)
    except (OSError, ValueError):
        return raw


def _relative_to_workspace(path: Path, workspace_dir: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
