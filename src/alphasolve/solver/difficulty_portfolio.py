"""Worker obstacle portfolio helpers.

This module preserves bounded worker-local obstacles for the orchestrator, research
reviewer, process audit, and curator. Canonical difficulty identity remains
curator-owned.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


_MAX_HANDOFFS = 12
_MAX_TEXT = 2000


def candidate_handoffs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deduplicated obstacle handoffs in stable encounter order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_handoff = record.get("difficulty_handoff")
        handoff = raw_handoff if isinstance(raw_handoff, dict) else record
        if not isinstance(handoff, dict):
            continue
        if not str(handoff.get("obstacle") or "").strip():
            continue
        worker_id = str(handoff.get("worker_id") or record.get("worker_id") or "").strip()
        if not worker_id or worker_id in seen:
            continue
        seen.add(worker_id)
        out.append(_compact_handoff(handoff))
        if len(out) >= _MAX_HANDOFFS:
            break
    return out


def load_recent_candidate_handoffs(outcomes_path: Path, *, limit: int = _MAX_HANDOFFS) -> list[dict[str, Any]]:
    """Read durable audit outcomes without failing live scheduling on malformed history."""
    try:
        lines = outcomes_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
        if len(records) >= max(limit * 3, limit):
            break
    records.reverse()
    return candidate_handoffs(records)[-limit:]


def merge_candidate_handoffs(*collections: Iterable[dict[str, Any]], limit: int = _MAX_HANDOFFS) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for collection in collections:
        records.extend(collection)
    return candidate_handoffs(records)[-limit:]


def review_batch_key(handoffs: Iterable[dict[str, Any]]) -> str:
    return ":".join(sorted(str(item.get("worker_id") or "") for item in handoffs if item.get("worker_id")))


def build_reviewer_prompt(handoffs: list[dict[str, Any]]) -> str:
    """Build a bounded, evidence-oriented reviewer request for obstacle comparison."""
    payload = json.dumps(handoffs, ensure_ascii=False, indent=2)
    return (
        "Review the following worker obstacle portfolio before recommending any new worker. These are local observations, "
        "not canonical graph structure; read cited evidence when needed. `obstacle_records` may include task-specific reasoning "
        "observations, so compare their delegated scope, concrete obstacles, and evidence rather than wording. "
        "`delivered_instead` states how far each attempt actually got against its assigned target, `obstacle_scope` marks a "
        "worker's own reading of whether the obstacle is local to its task or a global obstruction, and "
        "`obstacle_reporting` is `runtime_reconstructed_from_task_audit` when no role recorded the obstacle and the runtime "
        "substituted the audit's residual obligation. Treat routes reported as already excluded as tabu, and let the portfolio "
        "suggest directions no current node represents. Do not edit state or dispatch work.\n\n"
        "Return the standard reviewer report. Recommend one bounded next step, but do not invent canonical IDs or edit the DAG: "
        "the curator reconciles evidence at checkpoints.\n\n"
        "## Worker Obstacle Portfolio\n\n```json\n"
        + payload
        + "\n```"
    )


def _compact_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    def text(key: str) -> str:
        return " ".join(str(handoff.get(key) or "").split())[:_MAX_TEXT]

    return {
        "handoff_id": text("handoff_id"),
        "worker_id": text("worker_id"),
        "difficulty_id": text("difficulty_id"),
        "method_id": text("method_id"),
        "assigned_target": text("assigned_target"),
        "execution_status": text("execution_status"),
        "obstacle": text("obstacle"),
        "delivered_instead": text("delivered_instead"),
        "obstacle_scope": text("obstacle_scope") or "unclear",
        "obstacle_reporting": text("obstacle_reporting") or "worker_recorded",
        "obstacle_records": [
            {
                "role": " ".join(str(record.get("role") or "").split())[:100],
                "obstacle": " ".join(str(record.get("obstacle") or "").split())[:_MAX_TEXT],
                "delivered_instead": " ".join(str(record.get("delivered_instead") or "").split())[:_MAX_TEXT],
                "obstacle_scope": " ".join(str(record.get("obstacle_scope") or "").split())[:40],
                "delegated_description": " ".join(str(record.get("delegated_description") or "").split())[:_MAX_TEXT],
                "delegated_task": " ".join(str(record.get("delegated_task") or "").split())[:_MAX_TEXT],
            }
            for record in handoff.get("obstacle_records") or []
            if isinstance(record, dict) and str(record.get("obstacle") or "").strip()
        ][-8:],
        "evidence_refs": [str(item)[:_MAX_TEXT] for item in handoff.get("evidence_refs") or [] if str(item).strip()],
        "source_confidence": text("source_confidence"),
    }
