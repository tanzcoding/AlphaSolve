"""Validated research strategies returned by the independent reviewer."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .difficulty_dag import _METHOD_IDS, DifficultyDagStore
from .policy import DifficultyDagPolicy


_NEXT_STEP_KINDS = {"TARGET_NODE", "NEW_DIRECTION", "HOLD"}
_GRAPH_OBSERVATION_KINDS = {
    "EDGE_SUSPECT", "NODE_SCOPE_SUSPECT", "COMPONENT_STAGNANT", "STATUS_SUSPECT", "DUPLICATE_NODE",
    # A Statement, tabu, or route framing baked a specific proof architecture (gadget,
    # construction shape) into what should be a bare open/established mathematical claim,
    # without a verified proposition establishing that architecture is necessary.
    "SCAFFOLDING_ASSUMPTION_UNVERIFIED",
}
_GRAPH_EFFECTS = {"reconsider_edge", "supersede_node", "merge_candidate", "keep_independent", "restate_without_assumption"}

# ``kind`` preserves compatibility with older persisted plans.  ``selection_scope`` is
# the reviewer-owned policy layer: it tells the orchestrator whether this is a local
# repair, a route pivot within one node, an across-graph portfolio move, a genuine
# technique-level departure, or an original-problem synthesis.  Neither the curator
# nor the orchestrator is allowed to infer this classification from outcome counts.
_SELECTION_SCOPES = {
    "LOCAL_REPAIR",
    "NODE_ROUTE",
    "GRAPH_PORTFOLIO",
    "TECHNIQUE_EXPLORATION",
    "GLOBAL_SYNTHESIS",
}
_TABU_LEVELS = {"hard", "soft", "hint"}
# 反思结论只描述"上一版 proposal 与其证据的关系"，不描述新路线本身。
_REFLECTION_DECISIONS = {"CONTINUE", "LOCAL_REPAIR", "PIVOT", "PARK", "RETIRE"}

# 一条 `soft` tabu 本应表示"机制被真正尝试过、构造受阻"；但 reviewer 有时把"没有构造出来、
# 类比判断会被挡住"这种未经检验的主观预判也写成 soft，之后就被当作既定事实反复继承。这里
# 用纯字符串匹配（无需 LLM 推理）识别这类措辞，配合缺失 evidence_refs，把它自动降级为最弱的
# `hint`，防止"没试过"和"试过但受阻"在证据强度上被混为一谈。这是运行时的事实性归档，不是
# curator/reviewer 的策略推理。
_UNVERIFIED_MECHANISM_MARKERS = (
    "no construction produced",
    "no construction was produced",
    "not attempted",
    "never attempted",
    "no attempt was made",
    "not actually tried",
    "without an explicit attempt",
    "is expected to",
    "expected to be blocked",
    "expected to fail",
    "likely blocked",
    "likely fails",
    "presumably",
    "by analogy",
    "should be blocked",
    "assumed to",
    "we believe",
)
# `soft`/`hint` tabu 在没有新 verified evidence 的情况下持续存在的最大周期数；超过后在
# `stale_tabu_routes()` 中被标记，提醒下一轮 reviewer 必须显式回应是否重新打开。
_STALE_TABU_MIN_CYCLES = 5


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

    def plan_target_snapshot(
        self,
        *,
        frontier: dict[str, Any],
        recommendation: dict[str, Any],
    ) -> dict[str, dict[str, str]]:
        """Capture only the canonical semantics a proposal actually depends on.

        ``frontier_revision`` is intentionally broad: it changes for unrelated graph
        curation and for progress counters. Dispatch safety instead depends on a selected
        canonical target's identity, statement, status, and dispatch mode. Persisting
        this narrow snapshot lets a proposal survive irrelevant/supportive updates while
        still rejecting an altered or terminal target.
        """
        dispatchable = {
            str(item.get("difficulty_id") or ""): item
            for item in frontier.get("dispatchable") or []
            if isinstance(item, dict) and str(item.get("difficulty_id") or "")
        }
        research_plan = recommendation.get("research_plan") if isinstance(recommendation, dict) else {}
        snapshots: dict[str, dict[str, str]] = {}
        for track in research_plan.get("tracks") or [] if isinstance(research_plan, dict) else []:
            if not isinstance(track, dict):
                continue
            difficulty_id = str(track.get("difficulty_id") or "").strip()
            target = dispatchable.get(difficulty_id)
            if not difficulty_id or not isinstance(target, dict):
                continue
            snapshots[difficulty_id] = _target_semantics(target)
        return snapshots

    def revalidate_plan_targets(
        self,
        *,
        frontier_revision: str,
        target_snapshot: dict[str, Any] | None,
        selected_tracks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Rebase a proposal unless a selected canonical target changed semantically.

        A global revision mismatch alone is not a research contradiction. For each
        selected in-graph track, compare the captured target semantics against the
        current projection and run normal dispatch preflight. Out-of-graph exploration
        has no canonical target to invalidate here; its executed worker outcomes remain
        its evidence gate.
        """
        current = self.reviewer_frontier()
        current_revision = str(current.get("frontier_revision") or "")
        if str(frontier_revision or "") == current_revision:
            return {"allowed": True, "revalidation": "exact", "frontier_revision": current_revision}
        dispatchable = {
            str(item.get("difficulty_id") or ""): item
            for item in current.get("dispatchable") or []
            if isinstance(item, dict) and str(item.get("difficulty_id") or "")
        }
        snapshots = target_snapshot if isinstance(target_snapshot, dict) else {}
        affected: list[dict[str, Any]] = []
        for track in selected_tracks:
            difficulty_id = str(track.get("difficulty_id") or "").strip()
            if not difficulty_id:
                continue
            expected = snapshots.get(difficulty_id)
            current_target = dispatchable.get(difficulty_id)
            preflight = self.dispatch_preflight(
                difficulty_id=difficulty_id,
                method_id=str(track.get("method_id") or "") or None,
            )
            if not preflight.get("allowed") or not isinstance(current_target, dict):
                affected.append({"difficulty_id": difficulty_id, "reason": preflight.get("reason") or "target_not_dispatchable"})
                continue
            if not isinstance(expected, dict) or _target_semantics(current_target) != expected:
                affected.append({"difficulty_id": difficulty_id, "reason": "target_semantics_changed"})
        if affected:
            return {
                "allowed": False,
                "reason": "reviewer_plan_target_stale",
                "message": "A selected canonical target changed or is no longer actionable; request a fresh reviewer plan.",
                "affected_targets": affected,
                "frontier_revision": current_revision,
            }
        return {
            "allowed": True,
            "revalidation": "rebased_unaffected",
            "frontier_revision": current_revision,
        }

    def validate_frontier_revision(self, *, frontier_revision: str) -> dict[str, Any]:
        """Legacy strict check retained for integrations that do not provide plan targets."""
        current = self.reviewer_frontier()
        if str(frontier_revision or "") != str(current.get("frontier_revision") or ""):
            return {
                "allowed": False,
                "reason": "reviewer_plan_stale",
                "message": "The canonical frontier changed after reviewer planning; request a fresh reviewer plan.",
            }
        return {"allowed": True, "revalidation": "exact", "frontier_revision": str(current.get("frontier_revision") or "")}

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


def _target_semantics(target: dict[str, Any]) -> dict[str, str]:
    """The target fields whose change can invalidate an already-audited proposal."""
    return {
        "difficulty_id": str(target.get("difficulty_id") or ""),
        "statement": str(target.get("statement") or ""),
        "status": str(target.get("status") or ""),
        "dispatch_mode": str(target.get("dispatch_mode") or ""),
    }


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
    prior_proposal: dict[str, Any] | None = None,
    reflection_required: bool = False,
    stagnation_level: str = "NONE",
) -> str:
    stagnation_level = (stagnation_level or "NONE").upper()
    payload = json.dumps(
        {
            "worker_results": worker_results,
            "process_audit": process_audit or None,
            "prior_proposal": prior_proposal or None,
            "reflection_required": bool(reflection_required),
            "stagnation_level": stagnation_level,
            "frontier": frontier,
        },
        ensure_ascii=False,
        indent=2,
    )
    reflection_block = (
        "## Reflect on your previous proposal before proposing again\n"
        "`prior_proposal` names your most recent proposal and every `outcome_assessments` entry the process auditor produced "
        "after its execution. `process_audit.latest` carries the most recent portfolio verdict, terminal gap, repeated avoided "
        "obligation, and route-contract signals.\n"
        "Read them first and answer, in `research_plan.prior_proposal_review`: which hypothesis the previous proposal actually "
        "claimed, what the settled evidence showed, and which single retrospective decision that comparison supports — `CONTINUE`, "
        "`LOCAL_REPAIR`, `PIVOT`, `PARK`, or `RETIRE`. `RETIRE` asserts a refuted premise and requires cited verified evidence; "
        "repeated failure without a refutation is stagnation and supports `LOCAL_REPAIR`, `PIVOT`, or `PARK` instead.\n"
        "If the previous proposal's outcome audit was `STALLED` or `MISALIGNED`, state in `strategy` what you changed in "
        "response. Re-emitting the same route with the same first milestone, or renaming it, does not answer the audit.\n"
        + (
            "This retrospective is mandatory for this cycle: a plan without a valid `prior_proposal_review` is rejected.\n\n"
            if reflection_required
            else "There is no settled prior proposal yet, so this retrospective is optional for this cycle.\n\n"
        )
    )
    stagnation_block = ""
    if stagnation_level != "NONE":
        streak = int(((process_audit or {}).get("latest") or {}).get("stagnation_streak") or 0)
        stagnation_block = (
            "## Runtime stagnation escalation (fact, not a suggestion)\n"
            f"The runtime has counted {streak} consecutive outcome-audit checkpoints citing the same "
            f"`repeated_avoided_obligation` under a STALLED/MISALIGNED verdict. Current level: `{stagnation_level}`.\n"
            "This count is computed from immutable checkpoint decisions, not from an LLM's self-report of progress.\n"
            + (
                "At `FORCE_PIVOT`, this plan will be rejected at parse time unless its primary track's "
                "`selection_scope` is `GRAPH_PORTFOLIO` or `TECHNIQUE_EXPLORATION`. `LOCAL_REPAIR` and `NODE_ROUTE` "
                "are rejected as primary because they cannot by construction leave the mechanism that has already "
                "stalled this many times in a row. `GLOBAL_SYNTHESIS` is also rejected as primary here: attacking "
                "`problem.md` directly is not a substitute for diagnosing why the current architecture keeps hitting "
                "the same obligation, and consolidation on an un-repaired stalled route does not answer the audit.\n\n"
                if stagnation_level == "FORCE_PIVOT"
                else "This is advance warning: at `WATCH`/`ESCALATE` you are not yet blocked, but continuing to propose "
                "the same `LOCAL_REPAIR`/`NODE_ROUTE` primary on the same obligation without new evidence is exactly "
                "the pattern that will trigger `FORCE_PIVOT`. Consider whether a `GRAPH_PORTFOLIO` or "
                "`TECHNIQUE_EXPLORATION` challenger should be promoted to primary now, before it becomes mandatory.\n\n"
            )
        )
    top_down_block = (
        "## Decompose the terminal obligation top-down before selecting any route\n"
        "Before comparing local evidence, restate `problem.md`'s terminal obligation as an explicit set of necessary "
        "conditions that any successful route must eventually close — for example (adapt to the actual obligation, do not "
        "force this exact list): a precise target statement, any required construction/reduction and how its size/parameter "
        "scales, the forward/completeness direction, the converse/soundness direction that must hold for *every* admissible "
        "final object or counterexample candidate (not just the one the construction intends), and any threshold, bound, or "
        "equivalence step tying the construction back to the terminal claim. Name, for this cycle, which necessary condition "
        "is still open, which are already closed by cited verified propositions, and which one the primary track actually "
        "attacks. A route that only produces evidence for an already-closed condition, or that never engages the "
        "converse/soundness direction, is not closing the terminal gap regardless of local evidence volume.\n"
        "This decomposition is top-down: it fixes what must be proven before any worker asks whether one local construction "
        "is achievable. Do not let an unproven proof-architecture choice masquerade as an answered top-down obligation. "
        "Watch for language that quietly assumes a hard local step is achievable — \"a suitable gadget/construction can "
        "realize this\", \"a large enough penalty suffices\", \"independent components' contributions simply add\", \"the "
        "optimal solution can be assumed to fix/ignore X without loss of generality\", \"a standard construction should work "
        "here\". Treat such claims in a hypothesis, rationale, Statement, or tabu as a falsifiable scaffolding assumption "
        "(`SCAFFOLDING_ASSUMPTION_UNVERIFIED`), never as a settled step, unless a verified proposition already establishes "
        "it.\n\n"
    )
    return (
        "Review completed worker evidence and return one research plan, potentially with several independent research tracks. "
        "The difficulty DAG is a curator-owned evidence graph, not a route approval system. Do not invent canonical IDs, edit "
        "state, curate identities, dispatch workers, or prescribe worker-sized acceptance criteria.\n\n"
        + top_down_block
        + reflection_block
        + stagnation_block
        + "Your plan is directly executable: the orchestrator dispatches it through `ExecuteResearchPlan` without any "
        "pre-execution audit gate. State terminal-gap relevance, verified counterevidence, repeated blockers, and whether the "
        "current milestone is genuinely discriminating explicitly, because only a periodic, long-horizon process audit reviews "
        "the executed portfolio afterward — it never blocks this proposal.\n\n"
        "Use the complete graph, its component and node attempt statistics, recent methods/outcomes, verified-proposition links, "
        "worker results, local handoffs, process audits, the global `research_plan_execution_history`, and knowledge to choose the next mathematical direction freely. "
        "That history covers all persisted plans, not just the latest one: compare each plan's intended tracks with settled outcomes, "
        "verified proposition paths, task-audit residuals, and local difficulties before reusing a route. A route variant disproved by "
        "a verified proposition is tabu, but repeated lack of proof alone does not refute the node. The recent "
        "worker results are a realtime evidence delta not yet necessarily curated into the graph: inspect their `delivery`, "
        "`milestone_disposition`, `residual_obligation`, `rejection_locus`, and `route_label` before treating a verified result as route progress. "
        "Read periodic `route_contract_signals` as evidence about the reviewed milestone, never as an approved replacement route. A verified "
        "but `off_target` or `partial` result is not a delivered advance on its assigned route unless you explain, with evidence, how "
        "it supports the residual obligation. Treat a recently attempted `route_label` on the same node as tabu unless new evidence "
        "changes the target; changing only `method_id` while pursuing the same mathematical route is not a change of route. Prefer "
        "underexplored routes among otherwise comparable ones, but let terminal-gap relevance and delivered evidence override raw "
        "attempt counts.\n\n"
        "Evidence density is not the same as terminal relevance. Before continuing any route, state its best-case conclusion — the "
        "strongest claim it could establish if every remaining milestone succeeded — and check whether that claim actually entails "
        "the terminal obligation, or only a strictly weaker or distinct statement. A growing count of verified propositions on a "
        "route whose own best case is weaker than or distinct from the terminal claim is not evidence that it is closing the "
        "terminal gap, and does not by itself justify continuing to invest it as primary over a zero-evidence route whose best "
        "case does entail the terminal claim. Record this check in every track's `route_contract.terminal_sufficiency`, and "
        "separately record in `route_contract.falsification_condition` the specific test that would show the route's key "
        "mechanism does not work — this must engage the converse/soundness direction (arbitrary final object or "
        "counterexample class), not merely restate that the forward/completeness direction succeeded.\n\n"
        "Distinguish the mathematical fact from the proof-architecture assumption. A node's Statement should say only what claim "
        "is open or established; it must not silently bake in a specific construction or gadget as though that shape were itself "
        "required. If a Statement, tabu, or prior route framing fixes a particular proof architecture without a verified "
        "proposition establishing it is necessary, name that fixation explicitly as a graph observation of kind "
        "`SCAFFOLDING_ASSUMPTION_UNVERIFIED` rather than treating it as settled, and either propose a track that tests whether it "
        "is actually necessary or record it in `considered_but_deferred` with the reason it is not yet worth testing. A `soft` "
        "tabu (exhausted mechanism, not refuted) must never harden into an implicit requirement of the terminal obligation merely "
        "because every recent track has been shaped around it.\n\n"
        "Two runtime facts about tabu memory are precomputed for you and are not policy: `auto_downgraded_unverified=true` on a "
        "tabu rule means the runtime already found wording admitting the mechanism was predicted rather than actually attempted "
        "(no construction produced, likely/expected to fail, by analogy) with no evidence_refs, and downgraded it from `soft` to "
        "`hint` because it cannot be distinguished from an unverified scaffolding assumption; treat it as unproven, not exhausted. "
        "`stale=true` (with `cycles_since_created`) means this `soft`/`hint` route has persisted across many cycles with no plan "
        "ever attaching evidence_refs to it. A stale rule is not evidence it is wrong or right; it is a prompt that you must "
        "explicitly decide, in this plan, whether to reopen it (propose a track that actually tests it), re-affirm it with newly "
        "cited evidence, or explicitly re-park it with a reason in `strategy`. Silently continuing to avoid a stale route without "
        "addressing it is not acceptable.\n\n"
        "Before every plan, perform a portfolio retrospective over all plan/track executions and relevant DAG attempts. In `strategy`, "
        "state the reusable result of the comparison: what verified artifacts survive, which route premises or variants are disproved, "
        "which off-target/protocol failures are not mathematical evidence, whether distinct routes share a blocker, and whether the "
        "remaining obligation is a local repair or a reason to pivot. Apply that reflection to every track type, including `TARGET_NODE`; "
        "`NEW_DIRECTION` is not the default or the only response. When the Statement remains credible but no evidence-backed next "
        "attack exists, explicitly park the node/route in `strategy`, distinguish this from refutation, and name the proposition, bridge, "
        "construction, or falsifiable premise that would justify reopening it.\n\n"
        "For each serious recent route, diagnose the failed mechanism before choosing the next step. Do not replace `direct_proof` with "
        "`contradiction` merely to make a repeated route look new. If a local, named gap is repairable or a concrete bridge artifact "
        "would unlock the target, keep the route but recommend that artifact rather than rerunning the full task. If the same global "
        "obstacle recurs across genuinely different methods, reopen exploration under tabu: choose a route that avoids the failed "
        "mechanism, attacks a parent or ancestor, or is independent. If verified evidence can falsify the statement or a route's key "
        "assumption, prioritize a bounded counterexample or incompatibility check; do not call mere lack of proof a refutation. Continue "
        "to focus only when delivered evidence or a concrete repair path connects the route to the terminal gap.\n\n"
        "You are the sole policy owner for route reflection and search-level choice. Classify every track with `selection_scope`: "
        "LOCAL_REPAIR (same node and named bridge), NODE_ROUTE (same obligation, different mechanism), GRAPH_PORTFOLIO "
        "(cross-node/ancestor move), TECHNIQUE_EXPLORATION (leave the current graph and change mathematical framework), or "
        "GLOBAL_SYNTHESIS (attack problem.md by combining all evidence; method_id must be consolidation). For GRAPH_PORTFOLIO "
        "supply target_difficulty_ids; for TECHNIQUE_EXPLORATION and GLOBAL_SYNTHESIS supply terminal_obligation. Do not let "
        "the orchestrator, curator, task auditor, attempt counts, or an untried method make this choice for you.\n\n"
        "Every reviewer invocation is a portfolio review cycle. Periodically force a higher-order challenge by comparing the complete "
        "execution history and reviewer memory: alongside any local or portfolio primary, include an independent `challenger` at "
        "`TECHNIQUE_EXPLORATION` or `GLOBAL_SYNTHESIS` when you can state an evidence-backed mathematical question for it. It must avoid "
        "the dominant route/mechanism and reuse verified facts where helpful. Do not fabricate a high-level track merely to satisfy "
        "cadence: if none is live, explain the specific evidence-based reason in `strategy` or `considered_but_deferred`. You alone "
        "choose when this higher-order challenge is due, its scope, taboo, and reopen conditions.\n\n"
        "Maintain `tabu_rules` in the plan as your durable mathematical route memory. A hard tabu is a route premise refuted by "
        "verified evidence and needs evidence_refs plus a reopen_condition; a soft tabu is an exhausted mechanism with no local "
        "repair; a hint is a reusable failure explanation that later tracks must inherit. Link each track to the tabu_rule_ids it "
        "uses. A new method_id or new node name never escapes a tabu by itself.\n\n"
        "Your job is to choose route-level research contracts, not worker tasks. A track may be as hard as the current obstacle or span several "
        "worker turns. For every track, state the mathematical question, route identity, route hypothesis, required invariants, route-level "
        "success and failure conditions, a `terminal_sufficiency` statement of whether the route's best case entails the terminal obligation "
        "or only a weaker/distinct claim, and one to four ordered milestones. A milestone states a route-stage objective and evidence "
        "needed; it is not a worker-sized task or rubric. The first milestone is current; later milestones are conditional roadmap context "
        "and require a fresh reviewer cycle before execution. The orchestrator selects the current milestone, assigns available worker slots across "
        "tracks, turns that milestone into one bounded artifact, and commits acceptance criteria. A primary track "
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
        "    \"prior_proposal_review\": {\"proposal_id\": \"the previous plan id\", \"claimed_hypothesis\": \"what that plan claimed\", \"what_evidence_showed\": \"what settled evidence and audits established\", \"decision\": \"CONTINUE | LOCAL_REPAIR | PIVOT | PARK | RETIRE\", \"reason\": \"why that decision follows from the evidence\", \"evidence_refs\": [\"required for RETIRE\"]},\n"
        "    \"tracks\": [{\n"
        "      \"track_id\": \"stable short identifier unique within this plan\",\n"
        "      \"priority\": \"primary | challenger | supporting\",\n"
        "      \"kind\": \"TARGET_NODE | NEW_DIRECTION\",\n"
        "      \"selection_scope\": \"LOCAL_REPAIR | NODE_ROUTE | GRAPH_PORTFOLIO | TECHNIQUE_EXPLORATION | GLOBAL_SYNTHESIS\",\n"
        "      \"difficulty_id\": \"active canonical ID for LOCAL_REPAIR/NODE_ROUTE, otherwise empty\",\n"
        "      \"target_difficulty_ids\": [\"required for GRAPH_PORTFOLIO\"],\n"
        "      \"terminal_obligation\": \"required for TECHNIQUE_EXPLORATION/GLOBAL_SYNTHESIS\",\n"
        "      \"method_id\": \"direct_proof | contradiction | construction | computation | falsification | consolidation\",\n"
        "      \"route_label\": \"stable mathematical-route slug, e.g. exact-variance-contrapositive\",\n"
        "      \"tabu_rule_ids\": [\"reviewer tabu rules applied by this track\"],\n"
        "      \"reopen_condition\": \"what new evidence would make a parked/tabu route worth reconsidering\",\n"
        "      \"research_goal\": \"the mathematical question this route should resolve; not a worker-sized task\",\n"
        "      \"route_contract\": {\"hypothesis\": \"route-level mechanism\", \"required_invariants\": [\"properties every viable approach must preserve\"], \"success_condition\": \"route-level evidence that closes or advances the route\", \"failure_condition\": \"route-level evidence that parks the route\", \"terminal_sufficiency\": \"whether this route's best-case conclusion entails the terminal obligation or only a weaker/distinct claim\", \"falsification_condition\": \"the converse/soundness-side test (arbitrary counterexample/final-object class) that would show this route's key mechanism does not actually work\"},\n"
        "      \"milestones\": [{\"milestone_id\": \"stable-route-stage-id\", \"objective\": \"next route-stage question, not a worker task\", \"evidence_needed\": \"construction, theorem, counterexample, or finite check that distinguishes this stage\"}],\n"
        "      \"rationale\": \"why this route is live now and how it relates to the terminal gap\",\n"
        "      \"avoid\": \"known tabu routes, failed mechanisms, or conditions for changing this track\"\n"
        "    }],\n"
        "    \"tabu_rules\": [{\"tabu_id\": \"stable slug\", \"level\": \"hard | soft | hint\", \"route_label\": \"route slug\", \"mechanism\": \"failed mathematical mechanism\", \"applies_to\": \"NODE | IN_GRAPH | OUT_OF_GRAPH | GLOBAL | ALL\", \"evidence_refs\": [\"verified evidence for hard tabu\"], \"reopen_condition\": \"required for hard tabu\"}],\n"
        "    \"hold_reason\": \"required only when tracks is empty\"\n"
        "  },\n"
        "  \"graph_observations\": [{\"kind\": \"EDGE_SUSPECT | NODE_SCOPE_SUSPECT | COMPONENT_STAGNANT | STATUS_SUSPECT | DUPLICATE_NODE | SCAFFOLDING_ASSUMPTION_UNVERIFIED\", \"target_ids\": [\"canonical IDs\"], \"summary\": \"bounded graph concern; for SCAFFOLDING_ASSUMPTION_UNVERIFIED name the specific proof-architecture assumption baked into the Statement/tabu without a verified proposition establishing it is necessary\", \"evidence_refs\": [\"verified proposition, audit, or handoff path\"], \"recommended_graph_effect\": \"reconsider_edge | supersede_node | merge_candidate | keep_independent | restate_without_assumption\"}],\n"
        "  \"considered_but_deferred\": [{\"direction\": \"direction you evaluated and did not take\", \"reason\": \"why not now\"}]\n"
        "}\n```\n"
        "Return 1-4 tracks. A `primary` track is the main research investment; a `challenger` must be mathematically independent, "
        "not merely a new proof genre for the same route. `TARGET_NODE` requires an active canonical ID; `NEW_DIRECTION` may be "
        "outside the graph. `tracks: []` is allowed only with a concrete `hold_reason`, meaning no evidence-backed research track is "
        "currently justified. Use HOLD when the evidence cannot distinguish a live route: explain the unresolved decision, missing "
        "discriminating evidence, and future result that would justify a route contract. The orchestrator selects which tracks fit current "
        "worker slots, executes each selected track's current milestone through a bounded worker task, and writes its rubric. "
        "`considered_but_deferred` is optional but valuable: record any direction you "
        "seriously weighed and set aside, so a later reviewer inherits the judgement instead of repeating it. graph_observations are "
        "optional, must cite evidence, and never directly edit the graph. The curator later reconciles worker attempts and verified "
        "propositions into canonical progress.\n\n## Planning Input\n\n```json\n"
        + payload
        + "\n```"
    )


_STRATEGY_MARKER = re.compile(r"(?m)^\s*#{2,6}\s+Research Strategy JSON\s*$")

_STAGNATION_LEVELS = {"NONE", "WATCH", "ESCALATE", "FORCE_PIVOT"}
# At FORCE_PIVOT the primary track must leave both the stalled local mechanism
# (LOCAL_REPAIR/NODE_ROUTE) and the "attack problem.md directly" escape hatch
# (GLOBAL_SYNTHESIS): only a genuine portfolio or technique-level move counts as
# actually responding to the stall.
_FORCE_PIVOT_ALLOWED_PRIMARY_SCOPES = {"GRAPH_PORTFOLIO", "TECHNIQUE_EXPLORATION"}


def parse_recommendation(
    text: str,
    *,
    reflection_required: bool = False,
    stagnation_level: str = "NONE",
) -> dict[str, Any] | None:
    """Extract a reviewer-owned research plan from its final answer.

    The heading depth is matched loosely on purpose. The fenced JSON body is the actual
    contract, and the final marker wins because the reviewer may quote the template while
    reasoning. Legacy single-``next_step`` reports are normalized to a one-track plan so
    already-persisted reviewer history remains usable during the protocol transition.

    ``reflection_required`` is set by the runtime once a prior proposal has settled
    evidence. It makes ``prior_proposal_review`` mandatory, so a new proposal cannot
    silently bypass what the previous one already established or failed to established.

    ``stagnation_level`` is runtime-computed from consecutive outcome-audit checkpoints
    citing the same repeated blocker (see ``progress_audit._compute_stagnation_streak``).
    At ``FORCE_PIVOT`` the primary track's ``selection_scope`` is hard-rejected here unless
    it is ``GRAPH_PORTFOLIO`` or ``TECHNIQUE_EXPLORATION``.
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

    research_plan = _parse_research_plan(
        value, reflection_required=reflection_required, stagnation_level=stagnation_level
    )
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


def _parse_research_plan(
    value: dict[str, Any],
    *,
    reflection_required: bool = False,
    stagnation_level: str = "NONE",
) -> dict[str, Any] | None:
    stagnation_level = (stagnation_level or "NONE").upper()
    if stagnation_level not in _STAGNATION_LEVELS:
        stagnation_level = "NONE"
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
    prior_review = _parse_prior_proposal_review(
        raw_plan.get("prior_proposal_review") if isinstance(raw_plan, dict) else value.get("prior_proposal_review")
    )
    if reflection_required and prior_review is None:
        return None
    tracks: list[dict[str, Any]] = []
    track_ids: set[str] = set()
    for index, raw_track in enumerate(raw_tracks):
        track = _parse_research_track(raw_track, index=index)
        if track is None or track["track_id"] in track_ids:
            return None
        track_ids.add(track["track_id"])
        tracks.append(track)
    tabu_rules = _parse_tabu_rules(raw_plan.get("tabu_rules") if isinstance(raw_plan, dict) else None)
    tabu_ids = {rule["tabu_id"] for rule in tabu_rules}
    if any(not set(track["tabu_rule_ids"]).issubset(tabu_ids) for track in tracks):
        return None
    hard_tabu_routes = {rule["route_label"] for rule in tabu_rules if rule["level"] == "hard"}
    if any(track["route_label"] in hard_tabu_routes for track in tracks):
        return None
    if stagnation_level == "FORCE_PIVOT":
        # A HOLD plan (no tracks) is a legitimate response to a forced pivot -- it means
        # the reviewer is explicitly pausing rather than re-issuing the stalled mechanism.
        # Any plan that does propose tracks, though, must have its primary track actually
        # leave the mechanism that has stalled repeatedly.
        primary_tracks = [track for track in tracks if track["priority"] == "primary"]
        if primary_tracks and any(
            track["selection_scope"] not in _FORCE_PIVOT_ALLOWED_PRIMARY_SCOPES for track in primary_tracks
        ):
            return None
    if not tracks and not hold_reason:
        return None
    return {
        "objective": objective,
        "strategy": strategy,
        "prior_proposal_review": prior_review or {},
        "tracks": tracks,
        "tabu_rules": tabu_rules,
        "hold_reason": hold_reason,
    }


def _parse_prior_proposal_review(value: Any) -> dict[str, Any] | None:
    """Validate the reviewer's structured reflection on its previous proposal.

    This is a factual retrospective, not a new route: it records what the previous
    proposal claimed, what the evidence actually showed, and which of continue, local
    repair, pivot, park, or retire that comparison supports. ``RETIRE`` asserts that a
    route premise is refuted, so it must cite the verified evidence that refutes it;
    repeated failure alone is stagnation, not refutation.
    """
    if not isinstance(value, dict):
        return None
    proposal_id = _clean_text(value.get("proposal_id"), limit=80)
    claimed_hypothesis = _clean_text(value.get("claimed_hypothesis"), limit=3000)
    what_evidence_showed = _clean_text(value.get("what_evidence_showed"), limit=3000)
    decision = _clean_text(value.get("decision"), limit=40).upper()
    reason = _clean_text(value.get("reason"), limit=3000)
    evidence_refs = [
        _clean_text(item, limit=2000)
        for item in value.get("evidence_refs") or []
        if _clean_text(item, limit=2000)
    ] if isinstance(value.get("evidence_refs"), list) else []
    if (
        not proposal_id
        or not claimed_hypothesis
        or not what_evidence_showed
        or decision not in _REFLECTION_DECISIONS
        or (decision == "RETIRE" and not evidence_refs)
    ):
        return None
    return {
        "proposal_id": proposal_id,
        "claimed_hypothesis": claimed_hypothesis,
        "what_evidence_showed": what_evidence_showed,
        "decision": decision,
        "reason": reason,
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
    }


def _parse_research_track(value: Any, *, index: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = _clean_text(value.get("kind"), limit=80).upper()
    method_id = _clean_text(value.get("method_id"), limit=80)
    if kind not in {"TARGET_NODE", "NEW_DIRECTION"} or method_id not in _METHOD_IDS:
        return None
    difficulty_id = _clean_text(value.get("difficulty_id"), limit=80)
    route_label = _slug(value.get("route_label"), limit=80)
    research_goal = _clean_text(value.get("research_goal") or value.get("brief"), limit=4000)
    rationale = _clean_text(value.get("rationale") or value.get("why_now") or "", limit=3000)
    avoid = _clean_text(value.get("avoid"), limit=3000)
    priority = _clean_text(value.get("priority") or ("primary" if index == 0 else "challenger"), limit=40).lower()
    track_id = _slug(value.get("track_id") or route_label, limit=80)
    # Old persisted plans had only node-targeted and graph-external labels. Keep them
    # readable while requiring new reviewer plans to expose their actual decision level.
    default_scope = "NODE_ROUTE" if kind == "TARGET_NODE" else "TECHNIQUE_EXPLORATION"
    scope_explicit = bool(_clean_text(value.get("selection_scope"), limit=80))
    selection_scope = _clean_text(value.get("selection_scope") or default_scope, limit=80).upper()
    target_difficulty_ids = [
        _clean_text(item, limit=80)
        for item in value.get("target_difficulty_ids") or []
        if _clean_text(item, limit=80)
    ] if isinstance(value.get("target_difficulty_ids"), list) else []
    terminal_obligation = _clean_text(value.get("terminal_obligation"), limit=3000)
    tabu_rule_ids = [
        _slug(item, limit=80) for item in value.get("tabu_rule_ids") or []
        if _slug(item, limit=80)
    ] if isinstance(value.get("tabu_rule_ids"), list) else []
    reopen_condition = _clean_text(value.get("reopen_condition"), limit=2000)
    route_contract = _parse_route_contract(
        value.get("route_contract"),
        research_goal=research_goal,
        reopen_condition=reopen_condition,
    )
    milestones = _parse_route_milestones(value.get("milestones"), research_goal=research_goal)
    if (
        priority not in {"primary", "challenger", "supporting"}
        or selection_scope not in _SELECTION_SCOPES
        or not track_id
        or not route_label
        or not research_goal
        or route_contract is None
        or not milestones
        or (kind == "TARGET_NODE" and not difficulty_id)
        or (kind == "NEW_DIRECTION" and difficulty_id)
        or (selection_scope in {"LOCAL_REPAIR", "NODE_ROUTE"} and not difficulty_id)
        or (selection_scope in {"GRAPH_PORTFOLIO", "TECHNIQUE_EXPLORATION", "GLOBAL_SYNTHESIS"} and difficulty_id)
        or (scope_explicit and selection_scope == "GRAPH_PORTFOLIO" and not target_difficulty_ids)
        or (scope_explicit and selection_scope in {"TECHNIQUE_EXPLORATION", "GLOBAL_SYNTHESIS"} and not terminal_obligation)
        or (scope_explicit and selection_scope == "GLOBAL_SYNTHESIS" and method_id != "consolidation")
    ):
        return None
    return {
        "track_id": track_id,
        "priority": priority,
        "kind": kind,
        "selection_scope": selection_scope,
        "difficulty_id": difficulty_id,
        "target_difficulty_ids": target_difficulty_ids,
        "terminal_obligation": terminal_obligation,
        "method_id": method_id,
        "route_label": route_label,
        "tabu_rule_ids": list(dict.fromkeys(tabu_rule_ids)),
        "reopen_condition": reopen_condition,
        "research_goal": research_goal,
        "rationale": rationale,
        "avoid": avoid,
        "route_contract": route_contract,
        "milestones": milestones,
    }


def _parse_route_contract(
    value: Any,
    *,
    research_goal: str,
    reopen_condition: str,
) -> dict[str, Any] | None:
    """Normalize reviewer-owned route semantics without turning them into worker tasks."""
    if value is None:
        # Persisted plans predate the explicit contract. Preserve their history while
        # making the missing semantics visible as conservative legacy defaults.
        return {
            "hypothesis": research_goal,
            "required_invariants": [],
            "success_condition": research_goal,
            "failure_condition": reopen_condition or "No route-level failure condition was recorded.",
            "terminal_sufficiency": "Not recorded: this plan predates the terminal-sufficiency check.",
            "terminal_sufficiency_stated": False,
            "falsification_condition": "Not recorded: this plan predates the falsification-condition check.",
            "falsification_condition_stated": False,
            "legacy_inferred": True,
        }
    if not isinstance(value, dict):
        return None
    hypothesis = _clean_text(value.get("hypothesis"), limit=3000)
    success_condition = _clean_text(value.get("success_condition"), limit=3000)
    failure_condition = _clean_text(value.get("failure_condition"), limit=3000)
    # Evidence density (verified proposition count) is not terminal relevance: the reviewer
    # must state, for every track, whether this route's own best case entails the terminal
    # obligation or only a weaker/distinct claim. A missing statement is a real defect, but
    # it must not silently void the whole plan: dropping the plan here costs an entire
    # reviewer cycle and hides *why* it was dropped. Mark it instead so the omission stays
    # visible to the reviewer's own next-cycle retrospective and to the periodic outcome audit.
    terminal_sufficiency = _clean_text(value.get("terminal_sufficiency"), limit=3000)
    # Mirrors terminal_sufficiency: an explicit, converse/soundness-facing falsification test.
    # It is the guardrail against a route whose "evidence" only ever exercises the
    # forward/completeness direction it was designed to satisfy. Missing it is a real
    # defect but, like terminal_sufficiency, must stay visible rather than silently voiding
    # an otherwise usable plan.
    falsification_condition = _clean_text(value.get("falsification_condition"), limit=3000)
    invariants = [
        _clean_text(item, limit=1500)
        for item in value.get("required_invariants") or []
        if _clean_text(item, limit=1500)
    ] if isinstance(value.get("required_invariants"), list) else []
    if (
        not hypothesis
        or not success_condition
        or not failure_condition
        or len(invariants) > 8
    ):
        return None
    return {
        "hypothesis": hypothesis,
        "required_invariants": list(dict.fromkeys(invariants)),
        "success_condition": success_condition,
        "failure_condition": failure_condition,
        "terminal_sufficiency": terminal_sufficiency,
        "terminal_sufficiency_stated": bool(terminal_sufficiency),
        "falsification_condition": falsification_condition,
        "falsification_condition_stated": bool(falsification_condition),
        "legacy_inferred": False,
    }


def _parse_route_milestones(value: Any, *, research_goal: str) -> list[dict[str, str]]:
    """Keep route-stage evidence distinct from orchestrator-authored worker tasks."""
    if value is None:
        return [{
            "milestone_id": "first-discriminating-artifact",
            "objective": research_goal,
            "evidence_needed": "Produce or refute the route's first discriminating mathematical artifact.",
        }]
    if not isinstance(value, list) or not value or len(value) > 4:
        return []
    milestones: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return []
        milestone_id = _slug(raw.get("milestone_id"), limit=80)
        objective = _clean_text(raw.get("objective"), limit=3000)
        evidence_needed = _clean_text(raw.get("evidence_needed"), limit=3000)
        if not milestone_id or milestone_id in seen or not objective or not evidence_needed:
            return []
        seen.add(milestone_id)
        milestones.append({
            "milestone_id": milestone_id,
            "objective": objective,
            "evidence_needed": evidence_needed,
        })
    return milestones


def _parse_tabu_rules(value: Any) -> list[dict[str, Any]]:
    """Validate reviewer-owned route memory without converting it into DAG policy."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 24:
        return []
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return []
        tabu_id = _slug(raw.get("tabu_id") or raw.get("route_label"), limit=80)
        level = _clean_text(raw.get("level"), limit=40).lower()
        route_label = _slug(raw.get("route_label"), limit=80)
        mechanism = _clean_text(raw.get("mechanism"), limit=2000)
        evidence_refs = [
            _clean_text(item, limit=2000)
            for item in raw.get("evidence_refs") or []
            if _clean_text(item, limit=2000)
        ] if isinstance(raw.get("evidence_refs"), list) else []
        applies_to = _clean_text(raw.get("applies_to") or "ALL", limit=80).upper()
        reopen_condition = _clean_text(raw.get("reopen_condition"), limit=2000)
        if (
            not tabu_id or tabu_id in seen or level not in _TABU_LEVELS or not route_label
            or not mechanism or applies_to not in {"NODE", "IN_GRAPH", "OUT_OF_GRAPH", "GLOBAL", "ALL"}
            or (level == "hard" and (not evidence_refs or not reopen_condition))
        ):
            return []
        # A `soft` tabu asserts the mechanism was actually tried and is exhausted, not merely
        # predicted to fail. When its own wording admits no real attempt was made (or only an
        # analogical/likely-blocked judgement) and it carries no evidence_refs, it is
        # indistinguishable from an unverified scaffolding assumption. Downgrade it to `hint`
        # (the weakest level) rather than rejecting the whole plan: this is a factual strength
        # correction, not a policy judgement the runtime is not allowed to make.
        downgraded = False
        if level == "soft" and not evidence_refs and _looks_unverified(mechanism):
            level = "hint"
            downgraded = True
        seen.add(tabu_id)
        rules.append({
            "tabu_id": tabu_id,
            "level": level,
            "route_label": route_label,
            "mechanism": mechanism,
            "applies_to": applies_to,
            "evidence_refs": list(dict.fromkeys(evidence_refs)),
            "reopen_condition": reopen_condition,
            "auto_downgraded_unverified": downgraded,
            # Cycle-count fields are filled in by ``reviewer_strategy_memory`` once the rule's
            # full persisted history is known; a freshly parsed rule starts at 0.
            "cycles_since_created": 0,
            "stale": False,
        })
    return rules


def _looks_unverified(mechanism: str) -> bool:
    """Detect wording that admits a mechanism was predicted, not actually tried."""
    text = mechanism.lower()
    return any(marker in text for marker in _UNVERIFIED_MECHANISM_MARKERS)


def reviewer_strategy_memory(
    workspace_dir: Path,
    *,
    outcome_assessments: Mapping[str, list[dict[str, Any]]] | None = None,
    detailed_limit: int = 8,
) -> list[dict[str, Any]]:
    """Return durable reviewer decisions as read-only planning context.

    This is deliberately a projection of persisted reviewer plans, not a second policy
    store.  Worker outcomes remain immutable facts in the outcome ledger; the reviewer
    alone decides whether a prior tabu is still applicable or whether its documented
    reopen condition has been met.

    Memory is layered rather than truncated: the most recent ``detailed_limit`` proposals
    keep their route contracts and audit verdicts, while older ones collapse to one
    compact conclusion per proposal. A flat window would silently forget why an old route
    was retired; a flat full history would grow the planning prompt without bound.

    ``outcome_assessments`` is the post-execution audit mapping derived from the immutable
    records by the audit layer. It is passed in so the research-plan files stay
    single-writer.
    """
    plans_dir = Path(workspace_dir) / "curation_records" / "research_plans"
    try:
        paths = sorted(
            (path for path in plans_dir.glob("plan-*.json") if path.is_file()),
            key=lambda path: path.name,
        )
    except OSError:
        return []
    records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for path in paths:
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        recommendation = plan.get("recommendation") if isinstance(plan, dict) else None
        research_plan = recommendation.get("research_plan") if isinstance(recommendation, dict) else None
        if not isinstance(research_plan, dict):
            continue
        records.append((str(plan.get("plan_id") or path.stem), plan, research_plan))
    records.sort(key=lambda item: str(item[1].get("created_at") or ""))
    # Track, per non-hard route label, the cycle index where it was first raised and
    # whether any cycle ever attached evidence_refs to it. This is the recall/decay side
    # of tabu memory: a `soft`/`hint` route that has gone many cycles unrefreshed and was
    # never backed by evidence is flagged `stale` so the next reviewer must explicitly
    # decide whether to reopen it, instead of it silently persisting as settled fact.
    tabu_lifespan: dict[str, dict[str, Any]] = {}
    for index, (_plan_id, _plan, plan_research) in enumerate(records):
        for rule in plan_research.get("tabu_rules") or []:
            if not isinstance(rule, dict):
                continue
            route_label = str(rule.get("route_label") or "")
            level = str(rule.get("level") or "")
            if not route_label or level not in {"soft", "hint"}:
                continue
            entry = tabu_lifespan.setdefault(route_label, {"first_index": index, "evidence_seen": False})
            if rule.get("evidence_refs"):
                entry["evidence_seen"] = True

    def _annotate_tabu_rule(rule: dict[str, Any], *, current_index: int) -> dict[str, Any]:
        annotated = dict(rule)
        route_label = str(rule.get("route_label") or "")
        level = str(rule.get("level") or "")
        entry = tabu_lifespan.get(route_label)
        if level in {"soft", "hint"} and entry is not None:
            cycles = current_index - int(entry["first_index"])
            annotated["cycles_since_created"] = cycles
            annotated["stale"] = bool(cycles >= _STALE_TABU_MIN_CYCLES and not entry["evidence_seen"])
        else:
            annotated["cycles_since_created"] = 0
            annotated["stale"] = False
        return annotated

    limit = max(1, int(detailed_limit))
    detailed_from = max(0, len(records) - limit)
    memory: list[dict[str, Any]] = []
    for index, (plan_id, plan, research_plan) in enumerate(records):
        assessments = [
            dict(item)
            for item in (outcome_assessments or {}).get(plan_id, [])
            if isinstance(item, dict)
        ]
        prior_review = (
            dict(research_plan.get("prior_proposal_review") or {})
            if isinstance(research_plan.get("prior_proposal_review"), dict)
            else {}
        )
        if index < detailed_from:
            memory.append(_compact_strategy_memory(plan_id, plan, research_plan, assessments, prior_review))
            continue
        memory.append({
            "detail": "full",
            "plan_id": plan_id,
            "parent_proposal_id": str(plan.get("parent_proposal_id") or ""),
            "evidence_watermark": int(plan.get("evidence_watermark") or 0),
            "execution_status": str(plan.get("execution_status") or "planned"),
            "prior_proposal_review": prior_review,
            "outcome_assessments": assessments[-4:],
            "strategy": str(research_plan.get("strategy") or "")[:2000],
            "tabu_rules": [
                _annotate_tabu_rule(rule, current_index=index)
                for rule in research_plan.get("tabu_rules") or []
                if isinstance(rule, dict)
            ],
            "tracks": [
                {
                    "track_id": str(track.get("track_id") or ""),
                    "selection_scope": str(track.get("selection_scope") or ""),
                    "route_label": str(track.get("route_label") or ""),
                    "research_goal": str(track.get("research_goal") or "")[:1000],
                    "route_contract": dict(track.get("route_contract") or {}) if isinstance(track.get("route_contract"), dict) else {},
                    "milestones": [dict(item) for item in track.get("milestones") or [] if isinstance(item, dict)][:4],
                    "reopen_condition": str(track.get("reopen_condition") or "")[:1000],
                }
                for track in research_plan.get("tracks") or []
                if isinstance(track, dict)
            ],
        })
    return memory


def _compact_strategy_memory(
    plan_id: str,
    plan: dict[str, Any],
    research_plan: dict[str, Any],
    assessments: list[dict[str, Any]],
    prior_review: dict[str, Any],
) -> dict[str, Any]:
    """Collapse an older proposal to the conclusions a later reviewer still needs."""
    return {
        "detail": "compact",
        "plan_id": plan_id,
        "execution_status": str(plan.get("execution_status") or "planned"),
        "route_labels": [
            str(track.get("route_label") or "")
            for track in research_plan.get("tracks") or []
            if isinstance(track, dict) and str(track.get("route_label") or "")
        ],
        "outcome_verdicts": [str(item.get("verdict") or "") for item in assessments],
        "retrospective_decision": str(prior_review.get("decision") or ""),
        "conclusion": str(prior_review.get("what_evidence_showed") or research_plan.get("strategy") or "")[:400],
        # Hard tabu is the one durable constraint an old proposal still imposes.
        "hard_tabu_route_labels": [
            str(rule.get("route_label") or "")
            for rule in research_plan.get("tabu_rules") or []
            if isinstance(rule, dict) and str(rule.get("level") or "") == "hard"
        ],
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
