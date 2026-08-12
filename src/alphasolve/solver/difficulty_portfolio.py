"""Structured difficulty portfolio helpers.

This module deliberately performs no semantic clustering. It prepares bounded,
provenance-preserving worker difficulty handoffs for the orchestrator, research
reviewer, process audit, and curator. Canonical blocker identity remains curator-owned.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


_MAX_HANDOFFS = 12
_MAX_TEXT = 2000


def candidate_handoffs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deduplicated active handoffs in stable encounter order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_handoff = record.get("difficulty_handoff")
        handoff = raw_handoff if isinstance(raw_handoff, dict) else record
        if not isinstance(handoff, dict):
            continue
        if handoff.get("disposition") != "active_candidate":
            continue
        if not str(handoff.get("blocking_obligation") or "").strip():
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
    """Build a bounded, evidence-oriented reviewer request for difficulty comparison."""
    payload = json.dumps(handoffs, ensure_ascii=False, indent=2)
    return (
        "Review the following Difficulty Portfolio before recommending any new worker. These are worker-level "
        "handoffs reconciled only enough to preserve final evidence paths; they are not canonical blockers and may "
        "still be stale or distinct. Read cited evidence when needed. Compare exact obligations, last verified steps, "
        "and failed inference rather than wording. Do not edit state or dispatch work.\n\n"
        "Return the standard reviewer report, including the required `### Difficulty Comparison` section. Compare proposed "
        "parent_difficulty_id, relation_to_parent, and exact failed inference. Recommend one smallest executable obligation, "
        "but do not invent canonical IDs or edit the DAG: the curator performs that checkpoint-time merge.\n\n"
        "## Difficulty Portfolio\n\n```json\n"
        + payload
        + "\n```"
    )


def _compact_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    def text(key: str) -> str:
        return " ".join(str(handoff.get(key) or "").split())[:_MAX_TEXT]

    return {
        "worker_id": text("worker_id"),
        "source_difficulty_id": text("source_difficulty_id"),
        "parent_difficulty_id": text("parent_difficulty_id"),
        "relation_to_parent": text("relation_to_parent"),
        "parent_resolution_policy": text("parent_resolution_policy"),
        "difficulty_id": text("difficulty_id"),
        "method_id": text("method_id"),
        "assigned_target": text("assigned_target"),
        "child_delta": text("child_delta"),
        "execution_status": text("execution_status"),
        "target_status": text("target_status"),
        "disposition": text("disposition"),
        "blocking_obligation": text("blocking_obligation"),
        "last_verified_step": text("last_verified_step"),
        "why_current_route_fails": text("why_current_route_fails"),
        "suggested_attack": text("suggested_attack"),
        "dead_ends": text("dead_ends"),
        "evidence_refs": [str(item)[:_MAX_TEXT] for item in handoff.get("evidence_refs") or [] if str(item).strip()],
        "source_confidence": text("source_confidence"),
    }
