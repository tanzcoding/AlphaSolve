"""Validated reviewer recommendations for direct orchestration."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_PLAN_ACTIONS = {"DISPATCH_LEAF", "DISPATCH_NEW_DIRECTION", "NO_ACTION"}
_DISPATCH_MODES = {"direct", "consolidation"}
_METHOD_IDS = {
    "direct_proof",
    "contradiction",
    "construction",
    "computation",
    "falsification",
    "consolidation",
}
_GRAPH_OBSERVATION_KINDS = {"EDGE_SUSPECT", "NODE_SCOPE_SUSPECT", "COMPONENT_STAGNANT", "STATUS_SUSPECT", "DUPLICATE_NODE"}
_GRAPH_EFFECTS = {"reconsider_edge", "supersede_node", "merge_candidate", "keep_independent"}


def frontier_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose the full semantic graph and source indexes, never storage aliases."""
    dispatchable = [
        {
            "difficulty_id": str(item.get("difficulty_id") or "").strip(),
            "statement": str(item.get("statement") or "")[:4000],
            "dispatch_mode": str(item.get("dispatch_mode") or "direct"),
            "status": str(item.get("status") or "open"),
            "progress": dict(item.get("progress") or {}),
        }
        for item in snapshot.get("executable_difficulties") or []
        if isinstance(item, dict) and str(item.get("difficulty_id") or "").strip()
    ]
    projection = {
        "dispatchable": dispatchable,
        "graph": snapshot.get("reviewer_graph") if isinstance(snapshot.get("reviewer_graph"), dict) else {},
        "sources": snapshot.get("reviewer_sources") if isinstance(snapshot.get("reviewer_sources"), dict) else {},
        "curation_required": bool(snapshot.get("pending_checkpoints")),
        "pending_checkpoint_ids": [str(item) for item in snapshot.get("pending_checkpoints") or []],
        "global_attack_ready": bool((snapshot.get("global_consolidation_directive") or {}).get("ready")),
    }
    projection["frontier_revision"] = hashlib.sha256(
        json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return projection


def reviewer_prompt(*, worker_results: list[dict[str, Any]], frontier: dict[str, Any]) -> str:
    payload = json.dumps({"worker_results": worker_results, "frontier": frontier}, ensure_ascii=False, indent=2)
    return (
        "Review completed worker evidence and recommend exactly one next action. The difficulty DAG is a curator-owned "
        "evidence graph, not a route approval system. Do not invent canonical IDs, edit state, curate identities, or dispatch workers. "
        "You may select a current executable leaf or open one bounded independent direction. A new direction need not be a child "
        "of an old method unless evidence establishes that dependency.\n\n"
        "Use the complete graph, source indexes, and recent worker evidence together. Evidence priority is strict: verified propositions "
        "establish mathematical claims; the DAG records the current canonical hypothesis and attempt history but may be wrong; knowledge "
        "is a navigation aid and never by itself proves a claim, edge, or status. Worker attempts, including repeated no-progress attempts, "
        "are facts for the curator to archive; they are not a reason to force a split. Do not select refuted or superseded nodes for ordinary dispatch.\n\n"
        "End your answer with exactly one `### Planning Recommendation JSON` heading followed by one fenced JSON object:\n"
        "```json\n{\n"
        "  \"action\": \"DISPATCH_LEAF | DISPATCH_NEW_DIRECTION | NO_ACTION\",\n"
        "  \"difficulty_id\": \"an ID from frontier.dispatchable, otherwise empty\",\n"
        "  \"dispatch_mode\": \"direct | consolidation | empty\",\n"
        "  \"method_id\": \"direct_proof | contradiction | construction | computation | falsification | consolidation | empty\",\n"
        "  \"reason\": \"concise evidence-based rationale\",\n"
        "  \"exploration_brief\": \"precise independent direction, target, and evidence boundary; required only for DISPATCH_NEW_DIRECTION\",\n"
        "  \"graph_observations\": [{\"kind\": \"EDGE_SUSPECT | NODE_SCOPE_SUSPECT | COMPONENT_STAGNANT | STATUS_SUSPECT | DUPLICATE_NODE\", \"target_ids\": [\"canonical IDs\"], \"summary\": \"bounded graph concern\", \"evidence_refs\": [\"verified proposition, audit, or handoff path\"], \"recommended_graph_effect\": \"reconsider_edge | supersede_node | merge_candidate | keep_independent\"}]\n"
        "}\n```\n"
        "Use DISPATCH_LEAF only for a projected executable ID. Use DISPATCH_NEW_DIRECTION only with a concrete brief that does not "
        "repeat a refuted inference. graph_observations are optional, must cite evidence, and never directly edit the graph. The orchestrator "
        "executes valid plans; the curator later verifies observations and reconciles worker attempts and verified propositions into canonical progress.\n\n## Planning Input\n\n```json\n"
        + payload
        + "\n```"
    )


def parse_recommendation(text: str) -> dict[str, Any] | None:
    marker = "### Planning Recommendation JSON"
    match = re.search(r"```json\s*(\{.*?\})\s*```", text[text.find(marker):], flags=re.DOTALL) if marker in text else None
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    action = str(value.get("action") or "").strip().upper()
    dispatch_mode = str(value.get("dispatch_mode") or "").strip()
    method_id = str(value.get("method_id") or "").strip()
    reason = " ".join(str(value.get("reason") or "").split())[:4000]
    exploration_brief = " ".join(str(value.get("exploration_brief") or "").split())[:4000]
    if action not in _PLAN_ACTIONS or (dispatch_mode and dispatch_mode not in _DISPATCH_MODES):
        return None
    if method_id and method_id not in _METHOD_IDS:
        return None
    observations = _parse_graph_observations(value.get("graph_observations"))
    if observations is None or not reason or (action == "DISPATCH_NEW_DIRECTION" and not exploration_brief):
        return None
    return {
        "action": action,
        "difficulty_id": str(value.get("difficulty_id") or "").strip(),
        "dispatch_mode": dispatch_mode,
        "method_id": method_id,
        "reason": reason,
        "exploration_brief": exploration_brief,
        "graph_observations": observations,
    }


def _parse_graph_observations(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        return None
    observations: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "").strip().upper()
        effect = str(raw.get("recommended_graph_effect") or "").strip()
        target_ids = raw.get("target_ids")
        evidence_refs = raw.get("evidence_refs")
        summary = " ".join(str(raw.get("summary") or "").split())[:4000]
        if (
            kind not in _GRAPH_OBSERVATION_KINDS
            or effect not in _GRAPH_EFFECTS
            or not isinstance(target_ids, list)
            or not target_ids
            or len(target_ids) > 8
            or not isinstance(evidence_refs, list)
            or not evidence_refs
            or len(evidence_refs) > 16
            or not summary
        ):
            return None
        targets = [" ".join(str(item).split())[:80] for item in target_ids]
        refs = [" ".join(str(item).split())[:2000] for item in evidence_refs]
        if not all(targets) or not all(refs):
            return None
        observations.append({
            "kind": kind,
            "target_ids": targets,
            "summary": summary,
            "evidence_refs": refs,
            "recommended_graph_effect": effect,
        })
    return observations
