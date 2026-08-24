"""Validated research strategies returned by the independent reviewer."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .difficulty_dag import _METHOD_IDS, DifficultyDagStore
from .policy import DifficultyDagPolicy


_NEXT_STEP_KINDS = {"TARGET_NODE", "NEW_DIRECTION", "HOLD"}
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

    def dispatch_preflight(
        self,
        *,
        difficulty_id: str | None,
        method_id: str | None,
    ) -> dict[str, Any]:
        """Check one provenance difficulty ID against the canonical graph before dispatch.

        The orchestrator may cite a canonical ID as provenance without owning the graph.
        This keeps a terminal (resolved / refuted / superseded) obligation from being
        re-attacked silently, and surfaces the node's canonical statement plus any
        dispatch warnings so the worker receives the obligation it is actually assigned.
        """
        if not difficulty_id:
            return {"allowed": True}
        try:
            return self._dag.dispatch_preflight(difficulty_id=difficulty_id, method_id=method_id)
        except ValueError as exc:
            return {
                "allowed": False,
                "reason": "invalid_difficulty_id",
                "message": str(exc),
                "difficulty_id": difficulty_id,
            }

    def validate_frontier_revision(self, *, frontier_revision: str) -> dict[str, Any]:
        """Reject a research plan whose canonical evidence view has changed."""
        current = self.reviewer_frontier()
        if str(frontier_revision or "") != str(current.get("frontier_revision") or ""):
            return {
                "allowed": False,
                "reason": "reviewer_plan_stale",
                "message": "The canonical frontier changed after reviewer planning; request a fresh reviewer plan.",
            }
        return {"allowed": True}

    def validate_target(
        self,
        *,
        frontier_revision: str,
        difficulty_id: str,
        method_id: str | None,
    ) -> dict[str, Any]:
        revision = self.validate_frontier_revision(frontier_revision=frontier_revision)
        if not revision.get("allowed"):
            return revision
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
        "Review completed worker evidence and return one research plan, potentially with several independent research tracks. "
        "The difficulty DAG is a curator-owned evidence graph, not a route approval system. Do not invent canonical IDs, edit "
        "state, curate identities, dispatch workers, or prescribe worker-sized acceptance criteria.\n\n"
        "Use the complete graph, its component and node attempt statistics, recent methods/outcomes, verified-proposition links, "
        "worker results, local handoffs, process audits, and knowledge to choose the next mathematical direction freely. The recent "
        "worker results are a realtime evidence delta not yet necessarily curated into the graph: inspect their `delivery`, "
        "`residual_obligation`, `rejection_locus`, and `route_label` before treating a verified result as route progress. A verified "
        "but `off_target` or `partial` result is not a delivered advance on its assigned route unless you explain, with evidence, how "
        "it supports the residual obligation. Treat a recently attempted `route_label` on the same node as tabu unless new evidence "
        "changes the target; changing only `method_id` while pursuing the same mathematical route is not a change of route. Prefer "
        "underexplored routes among otherwise comparable ones, but let terminal-gap relevance and delivered evidence override raw "
        "attempt counts.\n\n"
        "For each serious recent route, diagnose the failed mechanism before choosing the next step. Do not replace `direct_proof` with "
        "`contradiction` merely to make a repeated route look new. If a local, named gap is repairable or a concrete bridge artifact "
        "would unlock the target, keep the route but recommend that artifact rather than rerunning the full task. If the same global "
        "obstacle recurs across genuinely different methods, reopen exploration under tabu: choose a route that avoids the failed "
        "mechanism, attacks a parent or ancestor, or is independent. If verified evidence can falsify the statement or a route's key "
        "assumption, prioritize a bounded counterexample or incompatibility check; do not call mere lack of proof a refutation. Continue "
        "to focus only when delivered evidence or a concrete repair path connects the route to the terminal gap.\n\n"
        "Your job is to choose research tracks, not worker tasks. A track may be as hard as the current obstacle or span several "
        "worker turns. For every track, state the mathematical question, the route identity, why it is live now, and the routes or "
        "mechanisms it must avoid. Do not turn the track into a worker-sized lemma, construction, or rubric: the orchestrator selects "
        "the first bounded artifact, assigns available worker slots across tracks, and commits acceptance criteria. A primary track "
        "may be accompanied by a genuinely independent challenger when the evidence justifies breadth; do not manufacture a second "
        "track by merely changing `method_id` on the same route.\n\n"
        "An external result you believe is already proved in the literature is usable evidence about where a route leads, even when "
        "you cannot cite it precisely from memory. Say so plainly in `research_strategy`, mark it as unverified external knowledge "
        "rather than established fact, and recommend reconstructing it in the workspace when that is the productive move. Do not "
        "downgrade a direction merely because it would mean rebuilding a known theorem.\n\n"
        "The runtime precomputes these facts for you; do not recount them. Each node's `progress` carries "
        "`method_attempt_counts` (attempts per method), `method_outcome_breakdown` (separate verified, delivered, and off-target "
        "verified yields), `consecutive_no_progress` (trailing attempts that did not deliver their assigned obligation), "
        "`last_verified_at`, `last_delivered_at`, `untried_methods`, and `attempted_routes` (route labels already tried, with their "
        "delivery outcomes). `graph.underexplored_pairs` lists active nodes with untried "
        "methods, ordered by how long they have gone without progress. These are neutral facts, not a ranking: a high "
        "`consecutive_no_progress` is evidence that the current route is exhausted, not proof that the node is wrong, and an "
        "untried method is not automatically worth trying — an untried label over an already-refuted route is not a new route.\n\n"
        "Only verified propositions establish mathematical claims. The DAG records canonical identity, relations, and attempt history "
        "but may be wrong; knowledge is a navigation aid and never by itself proves a claim, edge, or status. Do not target refuted "
        "or superseded nodes. A target node may be any active projected graph node; runtime performs the final safety check.\n\n"
        "End your answer with exactly one `### Research Strategy JSON` heading followed by one fenced JSON object:\n"
        "```json\n{\n"
        "  \"research_plan\": {\n"
        "    \"objective\": \"the portfolio-level mathematical objective for this review cycle\",\n"
        "    \"strategy\": \"evidence-based explanation of focus, decomposition, reopen, or refutation; include routes not to repeat\",\n"
        "    \"tracks\": [{\n"
        "      \"track_id\": \"stable short identifier unique within this plan\",\n"
        "      \"priority\": \"primary | challenger | supporting\",\n"
        "      \"kind\": \"TARGET_NODE | NEW_DIRECTION\",\n"
        "      \"difficulty_id\": \"active canonical graph ID for TARGET_NODE, otherwise empty\",\n"
        "      \"method_id\": \"direct_proof | contradiction | construction | computation | falsification | consolidation\",\n"
        "      \"route_label\": \"stable mathematical-route slug, e.g. exact-variance-contrapositive\",\n"
        "      \"research_goal\": \"the mathematical question this track should resolve; not a worker-sized task\",\n"
        "      \"rationale\": \"why this route is live now and how it relates to the terminal gap\",\n"
        "      \"avoid\": \"known tabu routes, failed mechanisms, or conditions for changing this track\"\n"
        "    }],\n"
        "    \"hold_reason\": \"required only when tracks is empty\"\n"
        "  },\n"
        "  \"graph_observations\": [{\"kind\": \"EDGE_SUSPECT | NODE_SCOPE_SUSPECT | COMPONENT_STAGNANT | STATUS_SUSPECT | DUPLICATE_NODE\", \"target_ids\": [\"canonical IDs\"], \"summary\": \"bounded graph concern\", \"evidence_refs\": [\"verified proposition, audit, or handoff path\"], \"recommended_graph_effect\": \"reconsider_edge | supersede_node | merge_candidate | keep_independent\"}],\n"
        "  \"considered_but_deferred\": [{\"direction\": \"direction you evaluated and did not take\", \"reason\": \"why not now\"}]\n"
        "}\n```\n"
        "Return 1-4 tracks. A `primary` track is the main research investment; a `challenger` must be mathematically independent, "
        "not merely a new proof genre for the same route. `TARGET_NODE` requires an active canonical ID; `NEW_DIRECTION` may be "
        "outside the graph. `tracks: []` is allowed only with a concrete `hold_reason`, meaning no evidence-backed research track is "
        "currently justified. The orchestrator selects which tracks fit current worker slots, decomposes each selected track into a "
        "bounded worker task, and writes its rubric. `considered_but_deferred` is optional but valuable: record any direction you "
        "seriously weighed and set aside, so a later reviewer inherits the judgement instead of repeating it. graph_observations are "
        "optional, must cite evidence, and never directly edit the graph. The curator later reconciles worker attempts and verified "
        "propositions into canonical progress.\n\n## Planning Input\n\n```json\n"
        + payload
        + "\n```"
    )


_STRATEGY_MARKER = re.compile(r"(?m)^\s*#{2,6}\s+Research Strategy JSON\s*$")


def parse_recommendation(text: str) -> dict[str, Any] | None:
    """Extract a reviewer-owned research plan from its final answer.

    The heading depth is matched loosely on purpose. The fenced JSON body is the actual
    contract, and the final marker wins because the reviewer may quote the template while
    reasoning. Legacy single-``next_step`` reports are normalized to a one-track plan so
    already-persisted reviewer history remains usable during the protocol transition.
    """
    markers = list(_STRATEGY_MARKER.finditer(text or ""))
    match = (
        re.search(r"```json\s*(\{.*?\})\s*```", text[markers[-1].end():], flags=re.DOTALL)
        if markers
        else None
    )
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None

    research_plan = _parse_research_plan(value)
    observations = _parse_graph_observations(value.get("graph_observations"))
    deferred = _parse_deferred_directions(value.get("considered_but_deferred"))
    if research_plan is None:
        return None
    parsed = {
        "research_plan": research_plan,
        "research_strategy": research_plan["strategy"],
        "graph_observations": observations,
        "considered_but_deferred": deferred,
    }
    # Transitional read compatibility for persisted reports and integrations. New code
    # must execute ``research_plan.tracks`` and must never treat this alias as the plan.
    if len(research_plan["tracks"]) == 1:
        parsed["next_step"] = dict(research_plan["tracks"][0])
    elif not research_plan["tracks"] and isinstance(value.get("next_step"), dict):
        parsed["next_step"] = {"kind": "HOLD", "difficulty_id": "", "method_id": "", "route_label": "", "brief": ""}
    return parsed


def _parse_research_plan(value: dict[str, Any]) -> dict[str, Any] | None:
    raw_plan = value.get("research_plan")
    if isinstance(raw_plan, dict):
        objective = _clean_text(raw_plan.get("objective"), limit=4000)
        strategy = _clean_text(raw_plan.get("strategy"), limit=6000)
        raw_tracks = raw_plan.get("tracks")
        hold_reason = _clean_text(raw_plan.get("hold_reason"), limit=2000)
    else:
        # Compatibility only: old reports described one executable step. Treat it as a
        # single research track, not as a preferred ongoing output schema. Old HOLD
        # reports remain a valid empty plan so persisted reviewer history still parses.
        objective = _clean_text(value.get("research_strategy"), limit=4000)
        strategy = _clean_text(value.get("research_strategy"), limit=6000)
        legacy_step = value.get("next_step")
        if isinstance(legacy_step, dict) and _clean_text(legacy_step.get("kind"), limit=80).upper() == "HOLD":
            raw_tracks = []
            hold_reason = strategy
        else:
            raw_tracks = [legacy_step]
            hold_reason = ""

    if not objective or not strategy or not isinstance(raw_tracks, list) or len(raw_tracks) > 4:
        return None
    tracks: list[dict[str, str]] = []
    track_ids: set[str] = set()
    for index, raw_track in enumerate(raw_tracks):
        track = _parse_research_track(raw_track, index=index)
        if track is None or track["track_id"] in track_ids:
            return None
        track_ids.add(track["track_id"])
        tracks.append(track)
    if not tracks and not hold_reason:
        return None
    return {
        "objective": objective,
        "strategy": strategy,
        "tracks": tracks,
        "hold_reason": hold_reason,
    }


def _parse_research_track(value: Any, *, index: int) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    kind = _clean_text(value.get("kind"), limit=80).upper()
    method_id = _clean_text(value.get("method_id"), limit=80)
    if kind not in {"TARGET_NODE", "NEW_DIRECTION"} or method_id not in _METHOD_IDS:
        return None
    difficulty_id = _clean_text(value.get("difficulty_id"), limit=80)
    route_label = _slug(value.get("route_label"), limit=80)
    # ``brief`` is accepted only for legacy history. New plans use ``research_goal`` to
    # make clear that the reviewer is naming a track, not a worker-sized assignment.
    research_goal = _clean_text(value.get("research_goal") or value.get("brief"), limit=4000)
    rationale = _clean_text(value.get("rationale") or value.get("why_now") or "", limit=3000)
    avoid = _clean_text(value.get("avoid"), limit=3000)
    priority = _clean_text(value.get("priority") or ("primary" if index == 0 else "challenger"), limit=40).lower()
    track_id = _slug(value.get("track_id") or route_label, limit=80)
    if (
        priority not in {"primary", "challenger", "supporting"}
        or not track_id
        or not route_label
        or not research_goal
        or (kind == "TARGET_NODE" and not difficulty_id)
    ):
        return None
    return {
        "track_id": track_id,
        "priority": priority,
        "kind": kind,
        "difficulty_id": difficulty_id,
        "method_id": method_id,
        "route_label": route_label,
        "research_goal": research_goal,
        "rationale": rationale,
        "avoid": avoid,
    }


def _parse_deferred_directions(value: Any) -> list[dict[str, str]]:
    """Keep the directions the reviewer evaluated and chose not to take.

    A reviewer may spend most of a call weighing a direction, reject it for a stated
    reason, and dispatch something else. Today that judgement lives only in its private
    reasoning and disappears with the log, so the next reviewer re-derives it from
    scratch and may discard the same direction for the same reason. Recording it costs
    nothing and turns a repeated deliberation into a citable prior decision. This is a
    log of considerations, not evidence: it never establishes mathematics and never
    forecloses re-examining a direction when new evidence arrives.
    """
    if not isinstance(value, list):
        return []
    deferred: list[dict[str, str]] = []
    for raw in value[:6]:
        if not isinstance(raw, dict):
            continue
        direction = _clean_text(raw.get("direction"), limit=1000)
        reason = _clean_text(raw.get("reason"), limit=1000)
        if not direction or not reason:
            continue
        deferred.append({"direction": direction, "reason": reason})
    return deferred


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
        # 数学路线的自命名标签。method_id 是证明体裁（只有 6 个固定值），同一条路线
        # 换个体裁标签就能规避 (node, method) 层面的重复检测，因此体裁无法承担
        # "这条路走过了"的记账。route_label 由 reviewer 自己命名，是唯一能表达路线
        # 同一性的键。
        "route_label": _slug(value.get("route_label"), limit=80),
        "brief": _clean_text(value.get("brief"), limit=4000),
    }


def _slug(value: Any, *, limit: int) -> str:
    """Normalize a self-named label so the same route spells identically across calls."""
    text = " ".join(str(value or "").split()).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit]


def _clean_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_graph_observations(value: Any) -> list[dict[str, Any]]:
    """Keep every well-formed observation and silently drop malformed ones.

    Observations are optional, advisory, and never edit the graph. A single malformed
    entry must not invalidate an otherwise usable strategy, because one reviewer call
    may have already consumed bounded reasoning and numerical-experiment budget.
    """
    if not isinstance(value, list):
        return []
    observations: list[dict[str, Any]] = []
    for raw in value[:8]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().upper()
        effect = str(raw.get("recommended_graph_effect") or "").strip()
        target_ids = raw.get("target_ids")
        evidence_refs = raw.get("evidence_refs")
        summary = " ".join(str(raw.get("summary") or "").split())[:4000]
        if (
            kind not in _GRAPH_OBSERVATION_KINDS
            or effect not in _GRAPH_EFFECTS
            or not isinstance(target_ids, list)
            or not isinstance(evidence_refs, list)
            or not summary
        ):
            continue
        targets = [text for text in (" ".join(str(item).split())[:80] for item in target_ids[:8]) if text]
        refs = [text for text in (" ".join(str(item).split())[:2000] for item in evidence_refs[:16]) if text]
        # 未引证据的观察对 curator 无法核对，等于噪声，直接丢弃。
        if not targets or not refs:
            continue
        observations.append({
            "kind": kind,
            "target_ids": targets,
            "summary": summary,
            "evidence_refs": refs,
            "recommended_graph_effect": effect,
        })
    return observations
