"""Validated research strategies returned by the independent reviewer."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .difficulty_dag import DifficultyDagStore
from .policy import DifficultyDagPolicy


_NEXT_STEP_KINDS = {"TARGET_NODE", "NEW_DIRECTION", "HOLD"}
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


class ReviewerPlanGateway:
    """Opaque runtime boundary between reviewer plans and the curator-owned DAG.

    The reviewer receives only a semantic projection. The orchestrator receives only
    a plan-validation result, never a frontier or raw canonical graph.
    """

    def __init__(self, workspace_dir: Path, *, policy: DifficultyDagPolicy | None = None) -> None:
        self._dag = DifficultyDagStore(workspace_dir, policy=policy)

    def reviewer_frontier(self) -> dict[str, Any]:
        return frontier_projection(self._dag.selection_snapshot())

    def validate_target(
        self,
        *,
        frontier_revision: str,
        difficulty_id: str,
        method_id: str | None,
    ) -> dict[str, Any]:
        current = self.reviewer_frontier()
        if str(frontier_revision or "") != str(current.get("frontier_revision") or ""):
            return {
                "allowed": False,
                "reason": "reviewer_plan_stale",
                "message": "The canonical frontier changed after reviewer planning; request a fresh reviewer plan.",
            }
        try:
            result = self._dag.dispatch_preflight(
                difficulty_id=difficulty_id,
                method_id=method_id,
            )
        except ValueError as exc:
            return {"allowed": False, "reason": "invalid_reviewer_target", "message": str(exc)}
        if not result.get("allowed"):
            return {
                "allowed": False,
                "reason": "reviewer_target_not_actionable",
                "message": "The reviewer-selected canonical target is no longer actionable; request a fresh reviewer plan.",
            }
        return result

    def global_attack_preflight(self) -> dict[str, Any]:
        return self._dag.global_attack_preflight()

    def begin_global_attack(self, *, worker_id: str) -> dict[str, Any]:
        return self._dag.begin_global_attack(worker_id=worker_id)

    def complete_global_attack(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._dag.complete_global_attack(**kwargs)


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


def reviewer_prompt(
    *,
    worker_results: list[dict[str, Any]],
    frontier: dict[str, Any],
    process_audit: dict[str, Any] | None = None,
) -> str:
    payload = json.dumps(
        {
            "worker_results": worker_results,
            "process_audit": process_audit or None,
            "frontier": frontier,
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "Review completed worker evidence and return exactly one research strategy. The difficulty DAG is a curator-owned "
        "evidence graph, not a route approval system. Do not invent canonical IDs, edit state, curate identities, or dispatch workers.\n\n"
        "Use the complete graph, its component and node attempt statistics, recent methods/outcomes, verified-proposition links, "
        "worker results, local handoffs, process audits, and knowledge to choose the next mathematical direction freely. When a leaf has "
        "repeatedly failed under the same or equivalent method without new verified evidence, you may change method, attack an active "
        "parent or ancestor from another angle, or choose a bounded independent direction. Treat recent repeated "
        "(node, method) attempts as tabu unless new evidence changes the target. Prefer underexplored node-method combinations among "
        "otherwise comparable routes, but let terminal-gap relevance and verified evidence override raw attempt counts.\n\n"
        "Only verified propositions establish mathematical claims. The DAG records canonical identity, relations, and attempt history "
        "but may be wrong; knowledge is a navigation aid and never by itself proves a claim, edge, or status. Do not target refuted "
        "or superseded nodes. A target node may be any active projected graph node; runtime performs the final safety check.\n\n"
        "End your answer with exactly one `### Research Strategy JSON` heading followed by one fenced JSON object:\n"
        "```json\n{\n"
        "  \"research_strategy\": \"concise evidence-based research direction, including the relevant DAG statistics, what to try, and what not to repeat\",\n"
        "  \"next_step\": {\n"
        "    \"kind\": \"TARGET_NODE | NEW_DIRECTION | HOLD\",\n"
        "    \"difficulty_id\": \"active canonical graph ID for TARGET_NODE, otherwise empty\",\n"
        "    \"method_id\": \"direct_proof | contradiction | construction | computation | falsification | consolidation | empty\",\n"
        "    \"brief\": \"precise bounded task for this worker; required unless HOLD\"\n"
        "  },\n"
        "  \"graph_observations\": [{\"kind\": \"EDGE_SUSPECT | NODE_SCOPE_SUSPECT | COMPONENT_STAGNANT | STATUS_SUSPECT | DUPLICATE_NODE\", \"target_ids\": [\"canonical IDs\"], \"summary\": \"bounded graph concern\", \"evidence_refs\": [\"verified proposition, audit, or handoff path\"], \"recommended_graph_effect\": \"reconsider_edge | supersede_node | merge_candidate | keep_independent\"}]\n"
        "}\n```\n"
        "`TARGET_NODE` requires an active canonical ID. `NEW_DIRECTION` requires a concrete bounded brief and need not be a child of "
        "an old method. `HOLD` means no evidence-backed bounded attempt is justified. graph_observations are optional, must cite "
        "evidence, and never directly edit the graph. The orchestrator executes valid next steps; the curator later reconciles "
        "worker attempts and verified propositions into canonical progress.\n\n## Planning Input\n\n```json\n"
        + payload
        + "\n```"
    )


def parse_recommendation(text: str) -> dict[str, Any] | None:
    marker = "### Research Strategy JSON"
    match = re.search(r"```json\s*(\{.*?\})\s*```", text[text.find(marker):], flags=re.DOTALL) if marker in text else None
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    research_strategy = _clean_text(value.get("research_strategy"), limit=6000)
    next_step = _parse_next_step(value.get("next_step"))
    observations = _parse_graph_observations(value.get("graph_observations"))
    if not research_strategy or next_step is None or observations is None:
        return None
    if next_step["kind"] == "TARGET_NODE" and not next_step["difficulty_id"]:
        return None
    if next_step["kind"] in {"TARGET_NODE", "NEW_DIRECTION"} and not next_step["brief"]:
        return None
    return {
        "research_strategy": research_strategy,
        "next_step": next_step,
        "graph_observations": observations,
    }


def _parse_next_step(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    kind = _clean_text(value.get("kind"), limit=80).upper()
    method_id = _clean_text(value.get("method_id"), limit=80)
    if kind not in _NEXT_STEP_KINDS or (method_id and method_id not in _METHOD_IDS):
        return None
    return {
        "kind": kind,
        "difficulty_id": _clean_text(value.get("difficulty_id"), limit=80),
        "method_id": method_id,
        "brief": _clean_text(value.get("brief"), limit=4000),
    }


def _clean_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


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
