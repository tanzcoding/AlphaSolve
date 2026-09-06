"""Human-readable snapshot of AlphaSolve's current research frontier."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .curation_records import read_recent_events
from .difficulty_dag import DifficultyDagStore
from .policy import DifficultyDagPolicy


STATE_FILE_NAME = "state.md"
_MAX_STATEMENT_CHARS = 1_200
_MAX_PROBLEM_CHARS = 4_000
_MAX_RECENT_OUTCOMES = 8
_MAX_EVIDENCE_REFS = 6
_MAX_VERIFIED_PATHS = 16


def research_frontier_state_path(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / "verified_propositions" / STATE_FILE_NAME


def write_research_frontier_state(
    workspace_dir: Path,
    *,
    runtime: dict[str, Any] | None = None,
    difficulty_dag_policy: DifficultyDagPolicy | None = None,
) -> Path:
    """Atomically publish the operator-facing research frontier snapshot."""
    workspace_dir = Path(workspace_dir)
    path = research_frontier_state_path(workspace_dir)
    content = render_research_frontier_state(
        workspace_dir,
        runtime=runtime,
        difficulty_dag_policy=difficulty_dag_policy,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    return path


def render_research_frontier_state(
    workspace_dir: Path,
    *,
    runtime: dict[str, Any] | None = None,
    difficulty_dag_policy: DifficultyDagPolicy | None = None,
) -> str:
    """Render current DAG, audit and runtime facts without treating them as proof."""
    workspace_dir = Path(workspace_dir)
    dag = DifficultyDagStore(workspace_dir, policy=difficulty_dag_policy)
    snapshot = dag.selection_snapshot()
    state = dag.load()
    audit = _read_json_object(workspace_dir / "progress_audit_state.json")
    latest_audit = audit.get("latest") if isinstance(audit.get("latest"), dict) else {}
    verified_paths = _verified_proposition_paths(workspace_dir)
    curation_events = _read_recent_events(workspace_dir, limit=80)
    lines = [
        "# Research Frontier State",
        "",
        "This is an automatically generated operational snapshot. It is not a verified proposition and is excluded from proposition counts.",
        f"Last refreshed: `{_now()}`",
        "",
        "## Original Problem",
        _read_text(workspace_dir / "problem.md", limit=_MAX_PROBLEM_CHARS) or "_problem.md is unavailable._",
        "",
        "## Current Best Verified Position",
        f"- Verified proposition files: `{len(verified_paths)}`",
        *(_bullet_paths(verified_paths[-_MAX_VERIFIED_PATHS:]) or ["- No verified proposition files are recorded."]),
        "",
        "## Current Research Frontier",
    ]
    lines.extend(_render_frontier(snapshot))
    lines.extend(["", "## Canonical Difficulty DAG"])
    lines.extend(_render_canonical_dag(state))
    lines.extend(["", "## Latest Process Audit"])
    lines.extend(_render_audit(latest_audit))
    lines.extend(["", "## Recent Difficulty Outcomes"])
    lines.extend(_render_recent_outcomes(state))
    lines.extend(["", "## Frontier Deviations"])
    lines.extend(_render_frontier_deviations(curation_events))
    lines.extend(["", "## Runtime Dispatch"])
    lines.extend(_render_runtime(runtime))
    lines.extend([
        "",
        "## Sources of Truth",
        "- Canonical difficulty evidence graph: `curation_records/difficulty_dag.json`",
        "- Process-audit records: `progress_audits/`",
        "- Established mathematics: `verified_propositions/` (excluding this file)",
        "- Hypotheses and lessons: `knowledge/`",
        "",
    ])
    return "\n".join(lines)


def _render_frontier(snapshot: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    pending = snapshot.get("pending_checkpoints") or []
    if pending:
        lines.append("- Pending curator checkpoints: " + ", ".join(f"`{item}`" for item in pending))

    leaves = snapshot.get("executable_difficulties") or []
    lines.extend(["", "### Executable Leaf Difficulties"])
    if not leaves:
        lines.append("- None currently executable.")
    else:
        for item in leaves:
            lines.extend(_render_difficulty(item))

    directive = snapshot.get("global_consolidation_directive") or {}
    lines.extend(["", "### Global Consolidation"])
    lines.append(f"- Ready: `{bool(directive.get('ready'))}`")
    if directive.get("reason"):
        lines.append(f"- State: `{directive['reason']}`")
    if directive.get("message"):
        lines.append(f"- Guidance: {_compact(directive['message'], 800)}")
    if directive.get("verified_proposition_count") is not None:
        lines.append(
            "- Verified propositions since baseline: "
            f"`{directive.get('verified_propositions_since_last_global_attack', 0)}` "
            f"of `{directive.get('verified_proposition_interval', '?')}` required"
        )
    attempt = directive.get("last_attempt")
    if isinstance(attempt, dict) and attempt.get("attempt_id"):
        lines.append(
            f"- Last attempt: `{attempt.get('attempt_id')}` — "
            f"phase=`{attempt.get('phase')}`, status=`{attempt.get('status')}`"
        )
    return lines


def _render_canonical_dag(state: dict[str, Any]) -> list[str]:
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    if not nodes:
        return ["- No curated difficulty nodes are recorded yet."]
    children: dict[str, list[str]] = {str(difficulty_id): [] for difficulty_id in nodes}
    for difficulty_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        for parent_id in node.get("parent_ids") or []:
            if parent_id in children:
                children[parent_id].append(str(difficulty_id))
    lines = [
        f"- Nodes: `{len(nodes)}`; revision: `{state.get('updated_at') or 'unknown'}`.",
        "- This section is a readable projection of `curation_records/difficulty_dag.json`.",
    ]
    for difficulty_id in sorted(nodes):
        node = nodes[difficulty_id]
        if not isinstance(node, dict):
            continue
        lines.append(
            f"- `{difficulty_id}` — status=`{node.get('status') or 'open'}`, "
            f"policy=`{node.get('resolution_policy') or 'manual'}`"
        )
        parent_ids = node.get("parent_ids") or []
        child_ids = sorted(children.get(str(difficulty_id), []))
        if parent_ids:
            lines.append("  - Parents: " + ", ".join(f"`{item}`" for item in parent_ids))
        if child_ids:
            lines.append("  - Children: " + ", ".join(f"`{item}`" for item in child_ids))
        lines.append(f"  - Obligation: {_compact(node.get('statement'), _MAX_STATEMENT_CHARS)}")
        progress = node.get("progress") if isinstance(node.get("progress"), dict) else {}
        if progress:
            lines.append(f"  - Attempts: `{progress.get('attempt_count', 0)}`")
            refs = progress.get("verified_proposition_refs") or []
            if refs:
                lines.append("  - Verified outputs: " + ", ".join(f"`{ref}`" for ref in refs[:_MAX_EVIDENCE_REFS]))
    return lines


def _render_frontier_deviations(events: list[dict[str, Any]]) -> list[str]:
    started: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    for event in events:
        worker_id = str(event.get("worker_id") or "").strip()
        if not worker_id:
            continue
        if event.get("kind") == "frontier_deviation_started":
            started[worker_id] = event
        elif event.get("kind") == "frontier_deviation_completed":
            completed[worker_id] = event
    worker_ids = sorted(set(started) | set(completed), key=lambda item: str((completed.get(item) or started.get(item) or {}).get("recorded_at") or ""), reverse=True)
    if not worker_ids:
        return ["- No explicit departures from the canonical frontier have been recorded."]
    lines: list[str] = []
    for worker_id in worker_ids[:_MAX_RECENT_OUTCOMES]:
        began = started.get(worker_id, {})
        finished = completed.get(worker_id, {})
        lines.append(
            f"- `{worker_id}` — status=`{finished.get('status') or 'running'}`"
        )
        if began.get("reason"):
            lines.append(f"  - Reason: {_compact(began['reason'], 900)}")
        leaves = began.get("executable_difficulty_ids") or []
        if leaves:
            lines.append("  - Canonical leaves at dispatch: " + ", ".join(f"`{item}`" for item in leaves))
        if finished:
            lines.append(f"  - Outcome: {_compact(finished.get('summary'), 900)}")
            if finished.get("verified_file"):
                lines.append(f"  - Verified output: `{finished['verified_file']}`")
            if finished.get("difficulty_handoff_file"):
                lines.append(f"  - Difficulty handoff: `{finished['difficulty_handoff_file']}`")
    return lines


def _render_difficulty(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return []
    difficulty_id = str(item.get("difficulty_id") or "unknown")
    lines = [
        f"- `{difficulty_id}` — status=`{item.get('status') or 'unknown'}`, "
        f"mode=`{item.get('dispatch_mode') or 'direct'}`, attempts=`{(item.get('progress') or {}).get('attempt_count', 0)}`",
        f"  - Obligation: {_compact(item.get('statement'), _MAX_STATEMENT_CHARS)}",
    ]
    parents = item.get("parent_difficulty_ids") or []
    if parents:
        lines.append("  - Parents: " + ", ".join(f"`{parent}`" for parent in parents))
    refs = [str(ref) for ref in item.get("evidence_refs") or [] if str(ref).strip()]
    if refs:
        lines.append("  - Evidence: " + ", ".join(f"`{ref}`" for ref in refs[:_MAX_EVIDENCE_REFS]))
    return lines


def _render_audit(latest: dict[str, Any]) -> list[str]:
    if not latest:
        return ["- No completed process audit is recorded yet."]
    lines = [
        f"- Checkpoint: `{latest.get('checkpoint_id') or 'unknown'}`",
        f"- Verdict: `{latest.get('verdict') or 'unknown'}`",
    ]
    if latest.get("terminal_gap"):
        lines.append(f"- Terminal gap: {_compact(latest['terminal_gap'], _MAX_STATEMENT_CHARS)}")
    if latest.get("audit_path"):
        lines.append(f"- Audit: `{latest['audit_path']}`")
    return lines


def _render_recent_outcomes(state: dict[str, Any]) -> list[str]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for difficulty_id, node in (state.get("nodes") or {}).items():
        progress = node.get("progress") if isinstance(node, dict) and isinstance(node.get("progress"), dict) else {}
        for attempt in progress.get("attempts") or []:
            if isinstance(attempt, dict):
                rows.append((str(attempt.get("recorded_at") or ""), str(difficulty_id), attempt))
    if not rows:
        return ["- No curator-archived worker attempts have been recorded yet."]
    return [
        f"- `{difficulty_id}` → `{attempt.get('status') or 'unknown'}` via `{attempt.get('method_id') or '-'}`"
        for _timestamp, difficulty_id, attempt in sorted(rows, reverse=True)[:_MAX_RECENT_OUTCOMES]
    ]


def _render_runtime(runtime: dict[str, Any] | None) -> list[str]:
    runtime = runtime or {}
    active = runtime.get("active_workers") or []
    if not active:
        return ["- No active-worker snapshot was supplied at the last refresh."]
    lines = [f"- Active workers: `{len(active)}`"]
    for worker in active:
        if not isinstance(worker, dict):
            continue
        lines.append(
            f"  - `{worker.get('worker_id') or 'unknown'}`: "
            f"difficulty=`{worker.get('difficulty_id') or 'free_exploration'}`, "
            f"method=`{worker.get('method_id') or '-'}`, {worker.get('progress') or 'running'}"
        )
    return lines


def _verified_proposition_paths(workspace_dir: Path) -> list[str]:
    root = workspace_dir / "verified_propositions"
    try:
        paths = sorted(
            path.relative_to(workspace_dir).as_posix()
            for path in root.rglob("*.md")
            if path.is_file() and path.name not in {"index.md", STATE_FILE_NAME}
        )
    except OSError:
        return []
    return paths


def _bullet_paths(paths: list[str]) -> list[str]:
    return [f"- `{path}`" for path in paths]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_recent_events(workspace_dir: Path, *, limit: int) -> list[dict[str, Any]]:
    path = Path(workspace_dir) / "curation_records" / "events.jsonl"
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()[-max(0, int(limit)):]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _read_text(path: Path, *, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit].strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "not recorded"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
