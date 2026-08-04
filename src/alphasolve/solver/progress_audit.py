"""Asynchronous, persisted strategic progress audits for AlphaSolve workers."""
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
from .research_state import ResearchStateStore
from .tool_runtime import build_solver_tool_registry
from .workspace_access import RoleWorkspaceAccess

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
    """Builds immutable outcome snapshots and audits them off the worker path.

    Worker execution only appends a small outcome record and enqueues work. The review
    itself runs in this queue's background thread, so a slow review never pauses worker
    execution or the orchestrator's dispatch loop.
    """

    DEFAULT_OUTCOMES_PER_AUDIT = 5
    DEFAULT_NO_PROGRESS_OUTCOMES_BEFORE_RESET = 10

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
        no_progress_outcomes_before_reset: int | None = None,
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
        self.no_progress_outcomes_before_reset = max(
            1,
            int(
                no_progress_outcomes_before_reset
                or self.DEFAULT_NO_PROGRESS_OUTCOMES_BEFORE_RESET
            ),
        )
        self.audit_runner = audit_runner
        self._queue: queue.Queue[ProgressAuditTask | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="progress-audit")
        self._lock = threading.RLock()
        self._started = False
        self._state = self._load_state()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for checkpoint_id in self._state.get("pending_checkpoints") or []:
                task = self._restore_pending_task(str(checkpoint_id))
                if task is not None:
                    self._queue.put(task)
            self._thread.start()

    def stop(self, timeout: float = 60.0) -> None:
        with self._lock:
            started = self._started
        if not started:
            return
        self._queue.put(None)
        self._thread.join(timeout=timeout)

    def record_outcome(self, payload: dict[str, Any]) -> bool:
        """Persist one terminal worker result and schedule an audit when due.

        This method is intentionally idempotent because it may be called from a future
        callback as well as a completion collector during shutdown.
        """
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            return False
        with self._lock:
            seen = set(self._state.get("recorded_worker_ids") or [])
            if worker_id in seen:
                return False
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

            last_scheduled = int(self._state.get("last_scheduled_outcome") or 0)
            if sequence - last_scheduled >= self.outcomes_per_audit:
                task = self._create_checkpoint_locked(watermark=sequence, previous_watermark=last_scheduled)
                self._state["last_scheduled_outcome"] = sequence
                pending = list(self._state.get("pending_checkpoints") or [])
                pending.append(task.checkpoint_id)
                self._state["pending_checkpoints"] = pending
                self._queue.put(task)
            self._save_state_locked()
        try:
            ResearchStateStore(self.layout.verified_dir).refresh_markdown_view()
        except Exception:
            # The generated Markdown view is observational; outcome durability must not depend on it.
            pass
        return True

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            latest = dict(self._state.get("latest") or {})
            completed = int(self._state.get("outcome_count") or 0)
            scheduled = int(self._state.get("last_scheduled_outcome") or 0)
            pending = list(self._state.get("pending_checkpoints") or [])
        return {
            "outcomes_recorded": completed,
            "outcomes_per_audit": self.outcomes_per_audit,
            "no_progress_outcomes_before_reset": self.no_progress_outcomes_before_reset,
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
            verdict, terminal_gap, repeated_blocker, recommendation = _parse_audit(audit_text)
            if verdict is None:
                status = "invalid_audit"
            decision = {
                "checkpoint_id": task.checkpoint_id,
                "watermark": task.watermark,
                "status": status,
                "verdict": verdict,
                "terminal_gap": terminal_gap,
                "repeated_avoided_obligation": repeated_blocker,
                "recommended_next_action": recommendation,
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
                "recommended_next_action": "",
                "error": str(exc),
                "evidence_path": _relative_to_workspace(task.evidence_path, self.layout.workspace_dir),
                "audit_path": _relative_to_workspace(task.checkpoint_dir / "audit.md", self.layout.workspace_dir),
                "created_at": _now_iso(),
            }

        audit_path = task.checkpoint_dir / "audit.md"
        audit_path.write_text(audit_text.strip() + "\n", encoding="utf-8")
        decision = self._finish_task(task, decision)
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

    def _finish_task(self, task: ProgressAuditTask, decision: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            pending = [
                item for item in self._state.get("pending_checkpoints") or []
                if item != task.checkpoint_id
            ]
            self._state["pending_checkpoints"] = pending
            verdict = str(decision.get("verdict") or "")
            if decision.get("status") == "completed" and verdict == "ADVANCING":
                no_progress_outcomes = 0
            elif decision.get("status") == "completed" and verdict in {"STALLED", "MISALIGNED"}:
                no_progress_outcomes = int(self._state.get("no_progress_outcomes") or 0) + (
                    task.watermark - task.previous_watermark
                )
            else:
                no_progress_outcomes = int(self._state.get("no_progress_outcomes") or 0)
            context_reset_required = no_progress_outcomes >= self.no_progress_outcomes_before_reset
            decision = {
                **decision,
                "no_progress_outcomes": no_progress_outcomes,
                "no_progress_threshold": self.no_progress_outcomes_before_reset,
                "context_reset_required": context_reset_required,
                "context_reset_handled": False,
            }
            self._state["no_progress_outcomes"] = no_progress_outcomes
            self._state["latest"] = decision
            self._state["updated_at"] = _now_iso()
            _write_json(task.checkpoint_dir / "decision.json", decision)
            self._save_state_locked()
            return decision

    def mark_context_reset_handled(self, checkpoint_id: str) -> None:
        """Persist that a fresh orchestrator session was scheduled for this checkpoint."""
        with self._lock:
            latest = dict(self._state.get("latest") or {})
            if latest.get("checkpoint_id") != checkpoint_id:
                return
            latest["context_reset_handled"] = True
            latest["context_reset_handled_at"] = _now_iso()
            self._state["latest"] = latest
            self._state["updated_at"] = _now_iso()
            self._save_state_locked()
            _write_json(self.layout.progress_audits_dir / checkpoint_id / "decision.json", latest)

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
        return agent.run(prompt, description="Periodic strategic progress audit").final_answer

    def _create_checkpoint_locked(self, *, watermark: int, previous_watermark: int) -> ProgressAuditTask:
        checkpoint_id = f"checkpoint-{watermark:04d}"
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
            "no_progress_outcomes_before_reset": self.no_progress_outcomes_before_reset,
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
        result_summary = _read_text(_safe_path(payload.get("result_summary_file")), limit=6000)
        difficulty = _read_text(_safe_path(payload.get("difficulty_declaration_file")), limit=4000)
        review = _read_text(_safe_path(payload.get("review_file")), limit=4000)
        handoff = payload.get("difficulty_handoff")
        if not isinstance(handoff, dict):
            handoff = _read_json_object(_safe_path(payload.get("difficulty_handoff_file")))
        if not result_summary:
            result_summary = str(payload.get("summary") or "")[:4000]
        return {
            "sequence": sequence,
            "recorded_at": _now_iso(),
            "worker_id": str(payload.get("worker_id") or ""),
            "status": str(payload.get("status") or "unknown"),
            "failure_kind": str(payload.get("failure_kind") or ""),
            "direction_id": payload.get("direction_id"),
            "gap_id": payload.get("gap_id"),
            "method_id": payload.get("method_id"),
            "pinned_target": str(payload.get("pinned_target") or "")[:2000],
            "worker_hint": str(payload.get("worker_hint") or "")[:2000],
            "orchestrator_session_id": payload.get("orchestrator_session_id"),
            "context_generation": payload.get("context_generation"),
            "rubric": str(payload.get("rubric") or "")[:4000],
            "summary": str(payload.get("summary") or "")[:4000],
            "result_summary": result_summary,
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
            "no_progress_outcomes": int(data.get("no_progress_outcomes") or 0),
            "latest": data.get("latest") if isinstance(data.get("latest"), dict) else {},
            "updated_at": str(data.get("updated_at") or ""),
        }

    def _save_state_locked(self) -> None:
        _write_json(self.layout.progress_audit_state_path, self._state)


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
    state = ResearchStateStore(layout.verified_dir).load()
    impacts = state.get("impacts") if isinstance(state.get("impacts"), dict) else {}
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
        "## Current Strategic State",
        _read_text(layout.verified_dir / "state.md", limit=12000) or "(state.md unavailable)",
        "",
        "## Newly Settled Outcomes",
    ]
    lines.extend(_render_outcomes(delta, layout.workspace_dir, impacts=impacts))
    lines.extend(["", "## Cumulative Outcome Ledger", *(_render_outcomes(visible, layout.workspace_dir, impacts=impacts))])
    return "\n".join(lines).rstrip() + "\n"


def _render_outcomes(
    outcomes: list[dict[str, Any]],
    workspace_dir: Path,
    *,
    impacts: dict[str, Any],
) -> list[str]:
    if not outcomes:
        return ["- None."]
    lines: list[str] = []
    for item in outcomes:
        worker_id = str(item.get("worker_id") or "")
        impact = impacts.get(worker_id) if isinstance(impacts.get(worker_id), dict) else {}
        lines.extend([
            f"### Outcome {item.get('sequence', '?')}: {item.get('status', 'unknown')}",
            f"- Direction / gap / method: `{item.get('direction_id') or '-'} / {item.get('gap_id') or '-'} / {item.get('method_id') or '-'}`",
            f"- Orchestrator session / context generation: `{item.get('orchestrator_session_id') or '-'} / {item.get('context_generation') if item.get('context_generation') is not None else '-'}`",
            f"- Failure kind: `{item.get('failure_kind') or '-'}`",
            f"- Solves original problem: `{bool(item.get('solved_problem'))}`",
        ])
        rubric = str(item.get("rubric") or "").strip()
        if rubric:
            lines.extend(["#### Pre-dispatch Rubric", rubric])
        if impact:
            lines.extend([
                "#### Orchestrator Impact Assessment",
                f"- Relation / gap effect / continuation: `{impact.get('relation_to_target') or '-'} / {impact.get('gap_effect') or '-'} / {impact.get('continuation_value') or '-'}`",
                f"- Assessment summary: {str(impact.get('summary') or '')[:1800]}",
            ])
            rubric_assessment = impact.get("rubric_assessment")
            if isinstance(rubric_assessment, dict):
                lines.append(
                    f"- Rubric score: `{rubric_assessment.get('passed_count', 0)}/{rubric_assessment.get('total_checks', 0)}`"
                )
                for check in rubric_assessment.get("checks") or []:
                    if isinstance(check, dict):
                        verdict = "pass" if check.get("passed") else "fail"
                        lines.append(
                            f"  - [{verdict}] {check.get('criterion')}: {check.get('evidence')}"
                        )
        handoff = item.get("difficulty_handoff")
        if isinstance(handoff, dict):
            lines.extend([
                "#### Structured Difficulty Handoff",
                f"- Disposition: `{handoff.get('disposition') or 'unknown'}`",
                f"- Exact obligation: {str(handoff.get('blocking_obligation') or 'not stated')[:1800]}",
                f"- Last verified step: {str(handoff.get('last_verified_step') or 'not stated')[:1800]}",
                f"- Why this route fails: {str(handoff.get('why_current_route_fails') or 'not stated')[:1800]}",
                f"- Suggested attack: {str(handoff.get('suggested_attack') or 'not stated')[:1800]}",
                f"- Evidence refs: {', '.join(str(ref) for ref in handoff.get('evidence_refs') or []) or 'none'}",
                "",
            ])
        for key, label, limit in (
            ("summary", "Runtime summary", 1800),
            ("result_summary", "Outcome summary", 3500),
            ("difficulty_declaration", "Avoided difficulty / blocker declaration", 2200),
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


def _render_manifest(manifest: dict[str, Any]) -> str:
    return "\n".join([
        f"# Progress Audit {manifest['checkpoint_id']}",
        "",
        f"- Outcome watermark: {manifest['watermark']}",
        f"- Previous watermark: {manifest['previous_watermark']}",
        f"- Trigger interval: {manifest['outcomes_per_audit']} settled outcomes",
        f"- Context-reset threshold: {manifest['no_progress_outcomes_before_reset']} no-progress outcomes",
        f"- Created at: {manifest['created_at']}",
        f"- Evidence: `{manifest['evidence_path']}`",
        "",
    ])


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
        "",
        "## Current Process Decision",
        f"- Verdict: `{decision.get('verdict') or 'unknown'}`",
        f"- Terminal gap: {decision.get('terminal_gap') or 'not stated'}",
        f"- Recommended next action: {decision.get('recommended_next_action') or 'not stated'}",
        f"- No-progress outcomes: `{decision.get('no_progress_outcomes', 0)}/{decision.get('no_progress_threshold', '?')}`",
        f"- Context reset requested: `{bool(decision.get('context_reset_required'))}`",
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
            f"- Previous recommended action: {previous.get('recommended_next_action') or 'not stated'}",
            "- Compare whether the terminal gap, outcome classifications, rubric scores, and avoided obligations changed substantively rather than cosmetically.",
        ])
    else:
        lines.append("- No earlier completed checkpoint is available; establish the first comparison baseline.")
    lines.extend([
        "",
        "## Recent Orchestrator Session Facts",
        *event_lines,
        "",
        "## Required Comparison Questions",
        "- Which attempts share the same target/gap but differ in method, rubric score, summary, or verification outcome?",
        "- Which correct propositions were incidental because their rubric or impact evidence did not connect them to the terminal gap?",
        "- Which avoided obligation repeats across multiple outcomes or context generations?",
        "- After any context reset, did the first new generation materially change target/rubric/method or merely rename the stalled route?",
        "- Which observed pattern is reusable enough to record as a strategy rule, and what evidence or exception bounds that rule?",
        "",
        "Do not copy internal worker/session identifiers into knowledge files. Use the raw artifacts only to derive de-identified, evidence-bounded lessons.",
    ])
    brief_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return brief_path


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
        return ["- No orchestrator session or reset facts recorded yet."]
    lines: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "unknown")
        if kind == "orchestrator_context_reset":
            lines.append(
                "- Context reset: process audit requested a new context generation; "
                f"verdict={event.get('verdict') or 'unknown'}, terminal gap={event.get('terminal_gap') or 'not stated'}."
            )
        elif kind == "orchestrator_session_started":
            lines.append("- A new orchestrator session began.")
        elif kind == "orchestrator_session_finished":
            lines.append(
                "- An orchestrator session ended: "
                f"solved={bool(event.get('solved'))}, worker_results={event.get('worker_results', 0)}."
            )
        elif kind == "worker_impact_recorded":
            lines.append(
                "- A worker impact was classified: "
                f"relation={event.get('relation_to_target') or 'unknown'}, "
                f"gap effect={event.get('gap_effect') or 'unknown'}, "
                f"rubric={event.get('rubric_passed_count') if event.get('rubric_passed_count') is not None else '?'}"
                f"/{event.get('rubric_total_checks') if event.get('rubric_total_checks') is not None else '?'}."
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
        "### Recommended Next Action\n"
        "### Cited Evidence\n\n"
        "Classify every new outcome as direct_advance, supporting, incidental, duplicate, or failed. "
        "A mathematically correct result is incidental unless you can cite how it closes a named terminal gap. "
        "State one precise next target, but do not prescribe a method family.\n\n"
        "If the repeated avoided obligation is concrete enough to give the curator a lead, append these exact candidate lines "
        "after the cited evidence (otherwise write NONE for all three):\n"
        "BLOCKER_DIRECTION_ID: existing-direction-id | NONE\n"
        "BLOCKER_GAP_ID: stable-candidate-gap-id | NONE\n"
        "BLOCKER_STATEMENT: exact unresolved mathematical obligation | NONE\n"
        "The auditor does not count occurrences, merge approaches, or dispatch work. The curator independently groups the "
        "outcome evidence into persistent blockers and chooses whether any gate should be active."
    )


def _parse_audit(text: str) -> tuple[str | None, str, dict[str, Any], str]:
    verdict_match = re.search(r"(?mi)^\s*VERDICT:\s*(ADVANCING|STALLED|MISALIGNED|INSUFFICIENT_EVIDENCE)\s*$", text)
    verdict = verdict_match.group(1).upper() if verdict_match else None
    if verdict not in _VALID_VERDICTS:
        verdict = None
    return (
        verdict,
        _extract_section(text, "Terminal Gap"),
        _extract_repeated_blocker(text),
        _extract_section(text, "Recommended Next Action"),
    )


def _extract_repeated_blocker(text: str) -> dict[str, str]:
    direction_match = re.search(r"(?mi)^\s*BLOCKER_DIRECTION_ID:\s*([A-Za-z0-9][A-Za-z0-9._-]*|NONE)\s*$", text)
    gap_match = re.search(r"(?mi)^\s*BLOCKER_GAP_ID:\s*([A-Za-z0-9][A-Za-z0-9._-]*|NONE)\s*$", text)
    statement_match = re.search(r"(?mi)^\s*BLOCKER_STATEMENT:\s*(.+?)\s*$", text)
    if not all((direction_match, gap_match, statement_match)):
        return {}
    direction_id = direction_match.group(1)
    gap_id = gap_match.group(1)
    statement = statement_match.group(1).strip()
    if direction_id == "NONE" or gap_id == "NONE" or statement == "NONE":
        return {}
    return {
        "direction_id": direction_id,
        "gap_id": gap_id,
        "statement": statement[:2000],
    }


def _render_blocker_brief(value: Any) -> str:
    if not isinstance(value, dict) or not value.get("statement"):
        return "no audit candidate recorded"
    return (
        f"Audit candidate `{value.get('direction_id')}/{value.get('gap_id')}` — "
        f"{value.get('statement')}\n\n"
        "The curator must independently decide persistence, semantic identity, occurrence count, and cross-route provenance."
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
