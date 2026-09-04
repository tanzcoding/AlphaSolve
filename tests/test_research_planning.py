from __future__ import annotations

import json

from alphasolve.solver.research_planning import (
    ReviewerPlanGateway,
    frontier_projection,
    parse_recommendation,
    reviewer_prompt,
    reviewer_strategy_memory,
)


def test_gateway_rebases_unrelated_frontier_update_but_blocks_changed_target():
    gateway = object.__new__(ReviewerPlanGateway)
    original = {
        "frontier_revision": "old",
        "dispatchable": [{
            "difficulty_id": "target",
            "statement": "Prove the bridge.",
            "status": "open",
            "dispatch_mode": "direct",
        }],
    }
    target_snapshot = gateway.plan_target_snapshot(
        frontier=original,
        recommendation={"research_plan": {"tracks": [{"difficulty_id": "target"}]}}
    )
    selected = [{"difficulty_id": "target", "method_id": "construction"}]

    # An unrelated graph/progress change altered the broad revision but not the selected target.
    gateway.reviewer_frontier = lambda: {
        "frontier_revision": "new-unrelated",
        "dispatchable": [{
            "difficulty_id": "target",
            "statement": "Prove the bridge.",
            "status": "open",
            "dispatch_mode": "direct",
        }],
    }
    gateway.dispatch_preflight = lambda **_kwargs: {"allowed": True}
    rebased = gateway.revalidate_plan_targets(
        frontier_revision="old",
        target_snapshot=target_snapshot,
        selected_tracks=selected,
    )
    assert rebased == {
        "allowed": True,
        "revalidation": "rebased_unaffected",
        "frontier_revision": "new-unrelated",
    }

    # A changed target statement is relevant evidence and must return to reviewer planning.
    gateway.reviewer_frontier = lambda: {
        "frontier_revision": "new-relevant",
        "dispatchable": [{
            "difficulty_id": "target",
            "statement": "Prove the strengthened bridge.",
            "status": "open",
            "dispatch_mode": "direct",
        }],
    }
    stale = gateway.revalidate_plan_targets(
        frontier_revision="old",
        target_snapshot=target_snapshot,
        selected_tracks=selected,
    )
    assert not stale["allowed"]
    assert stale["reason"] == "reviewer_plan_target_stale"
    assert stale["affected_targets"] == [{"difficulty_id": "target", "reason": "target_semantics_changed"}]


def test_strategy_memory_layers_older_proposals_into_compact_conclusions(tmp_path):
    plans_dir = tmp_path / "curation_records" / "research_plans"
    plans_dir.mkdir(parents=True)
    for index in range(5):
        (plans_dir / f"plan-{index:02d}.json").write_text(json.dumps({
            "plan_id": f"plan-{index:02d}",
            "created_at": f"2026-01-0{index + 1}T00:00:00+00:00",
            "execution_status": "executed",
            "recommendation": {"research_plan": {
                "objective": "Close the target.",
                "strategy": f"Strategy {index}.",
                "prior_proposal_review": {
                    "proposal_id": f"plan-{index - 1:02d}",
                    "decision": "PIVOT",
                    "what_evidence_showed": f"Route {index} hit the shared blocker.",
                },
                "tabu_rules": [{
                    "tabu_id": f"tabu-{index}",
                    "level": "hard",
                    "route_label": f"route-{index}",
                    "mechanism": "Refuted premise.",
                }],
                "tracks": [{"track_id": "t", "route_label": f"route-{index}", "research_goal": "Goal."}],
            }},
        }), encoding="utf-8")

    memory = reviewer_strategy_memory(
        tmp_path,
        outcome_assessments={"plan-00": [{"verdict": "STALLED"}]},
        detailed_limit=2,
    )

    assert [item["plan_id"] for item in memory] == [f"plan-{index:02d}" for index in range(5)]
    assert [item["detail"] for item in memory] == ["compact", "compact", "compact", "full", "full"]

    oldest = memory[0]
    assert oldest["outcome_verdicts"] == ["STALLED"]
    assert oldest["retrospective_decision"] == "PIVOT"
    assert oldest["hard_tabu_route_labels"] == ["route-0"]
    # A compact record drops the expensive detail but keeps the reusable conclusion.
    assert "tracks" not in oldest
    assert "route_contract" not in json.dumps(oldest)

    newest = memory[-1]
    assert newest["strategy"] == "Strategy 4."
    assert newest["tracks"][0]["route_label"] == "route-4"
    assert newest["prior_proposal_review"]["decision"] == "PIVOT"


def test_reflection_is_required_once_a_prior_proposal_has_audit_evidence():
    body = """### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Close the remaining bridge.",
    "strategy": "Keep the surviving mechanism and narrow its first artifact.",
    %s
    "tracks": [{
      "track_id": "bridge",
      "priority": "primary",
      "kind": "TARGET_NODE",
      "selection_scope": "LOCAL_REPAIR",
      "difficulty_id": "leaf",
      "method_id": "direct_proof",
      "route_label": "exact-variance",
      "research_goal": "Close the residual transfer step.",
      "route_contract": {
        "hypothesis": "The exact-variance mechanism survives the failed attempt.",
        "required_invariants": ["Keep the full interval constraint."],
        "success_condition": "The residual transfer step is proved.",
        "failure_condition": "A witness shows the mechanism cannot transfer."
      },
      "milestones": [{
        "milestone_id": "residual-transfer",
        "objective": "Decide the residual transfer step.",
        "evidence_needed": "A proof or a checked counterexample."
      }],
      "rationale": "The audit located a single missing bridge.",
      "avoid": "Do not rerun the full target."
    }]
  }
}
```"""
    review = (
        '"prior_proposal_review": {"proposal_id": "plan-prev", '
        '"claimed_hypothesis": "Exact variance alone closes the target.", '
        '"what_evidence_showed": "The worker delivered an off-target estimate and the process audit reported STALLED.", '
        '"decision": "LOCAL_REPAIR", "reason": "The mechanism survives; only the transfer step is missing."},'
    )

    assert parse_recommendation(body % "", reflection_required=True) is None
    assert parse_recommendation(body % "", reflection_required=False) is not None

    parsed = parse_recommendation(body % review, reflection_required=True)
    assert parsed is not None
    assert parsed["research_plan"]["prior_proposal_review"] == {
        "proposal_id": "plan-prev",
        "claimed_hypothesis": "Exact variance alone closes the target.",
        "what_evidence_showed": "The worker delivered an off-target estimate and the process audit reported STALLED.",
        "decision": "LOCAL_REPAIR",
        "reason": "The mechanism survives; only the transfer step is missing.",
        "evidence_refs": [],
    }

    # Retiring a route asserts a refuted premise, so it must cite verified evidence.
    retire_without_evidence = review.replace('"decision": "LOCAL_REPAIR"', '"decision": "RETIRE"')
    assert parse_recommendation(body % retire_without_evidence, reflection_required=True) is None

    retire_with_evidence = retire_without_evidence.replace(
        '"reason": "The mechanism survives; only the transfer step is missing."},',
        '"reason": "A verified witness refutes the premise.", "evidence_refs": ["verified_propositions/witness.md"]},',
    )
    retired = parse_recommendation(body % retire_with_evidence, reflection_required=True)
    assert retired is not None
    assert retired["research_plan"]["prior_proposal_review"]["evidence_refs"] == ["verified_propositions/witness.md"]


def test_reviewer_prompt_surfaces_prior_proposal_audits_and_reflection_contract():
    prompt = reviewer_prompt(
        worker_results=[],
        frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}},
        process_audit={"latest": {"verdict": "STALLED"}},
        prior_proposal={"plan_id": "plan-prev", "outcome_assessments": [{"verdict": "STALLED"}]},
        reflection_required=True,
    )

    assert "prior_proposal_review" in prompt
    assert "STALLED" in prompt
    assert "This retrospective is mandatory" in prompt
    assert "RETIRE" in prompt
    assert '"plan_id": "plan-prev"' in prompt

    optional = reviewer_prompt(
        worker_results=[],
        frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}},
    )
    assert "this retrospective is optional" in optional


def test_frontier_projection_includes_full_graph_and_source_indexes():
    projection = frontier_projection({
        "executable_difficulties": [{
            "difficulty_id": "leaf",
            "statement": "Prove the local estimate.",
            "dispatch_mode": "direct",
            "status": "open",
            "progress": {"attempt_count": 2},
        }],
        "reviewer_graph": {
            "nodes": [{"difficulty_id": "root", "child_ids": ["leaf"]}, {"difficulty_id": "leaf", "parent_ids": ["root"]}],
            "components": [{"root_ids": ["root"], "node_ids": ["root", "leaf"], "attempt_count": 2}],
        },
        "reviewer_sources": {
            "verified_propositions": [{"path": "verified_propositions/bridge.md", "title": "Bridge"}],
            "knowledge": [{"path": "knowledge/idea.md", "title": "Idea"}],
        },
        "pending_checkpoints": [],
        "global_consolidation_directive": {"ready": False},
    })
    assert projection["dispatchable"][0]["progress"]["attempt_count"] == 2
    assert projection["graph"]["nodes"][1]["parent_ids"] == ["root"]
    assert projection["sources"]["verified_propositions"][0]["path"] == "verified_propositions/bridge.md"


def test_strategy_accepts_active_parent_target():
    strategy = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_strategy": "The leaf has two failed contradiction attempts without new verified evidence. Attack its active parent by construction to test whether a global decomposition bypasses the local bridge.",
  "next_step": {
    "kind": "TARGET_NODE",
    "difficulty_id": "root",
    "method_id": "construction",
    "route_label": "parent-decomposition",
    "brief": "Construct a decomposition for the parent obligation or produce a counterexample to its current formulation."
  },
  "graph_observations": [{
    "kind": "EDGE_SUSPECT",
    "target_ids": ["root", "leaf"],
    "summary": "The verified counterexample contradicts the claimed prerequisite relation.",
    "evidence_refs": ["verified_propositions/counterexample.md"],
    "recommended_graph_effect": "reconsider_edge"
  }]
}
```""")
    assert strategy is not None
    assert strategy["research_strategy"].startswith("The leaf")
    assert strategy["next_step"]["difficulty_id"] == "root"
    assert strategy["next_step"]["method_id"] == "construction"
    assert strategy["graph_observations"][0]["kind"] == "EDGE_SUSPECT"


def test_strategy_requires_brief_for_dispatch_and_allows_hold_without_one():
    held = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_strategy": "No active route has an evidence-backed bounded next step.",
  "next_step": {"kind": "HOLD", "difficulty_id": "", "method_id": "", "brief": ""},
  "graph_observations": []
}
```""")
    assert held is not None
    assert held["next_step"]["kind"] == "HOLD"

    invalid = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_strategy": "Try a new independent route.",
  "next_step": {"kind": "NEW_DIRECTION", "difficulty_id": "", "method_id": "computation", "brief": ""},
  "graph_observations": []
}
```""")
    assert invalid is None


def test_strategy_requires_route_label_for_every_dispatchable_step():
    missing_route = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_strategy": "Retry the bridge by a different proof genre.",
  "next_step": {"kind": "TARGET_NODE", "difficulty_id": "bridge", "method_id": "contradiction", "brief": "Prove the bridge."}
}
```""")

    assert missing_route is None


def test_reviewer_prompt_uses_attempt_statistics_and_parent_attack_guidance():
    prompt = reviewer_prompt(worker_results=[], frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}})
    assert "verified propositions" in prompt
    assert "knowledge is a navigation aid" in prompt
    # Tabu is keyed on the mathematical route, not the proof genre: the same route
    # relabelled under another method_id must not read as unexplored.
    assert "`route_label`" in prompt
    assert "changing only `method_id`" in prompt
    assert "parent or ancestor" in prompt
    assert "next mathematical direction freely" in prompt
    assert "research_plan" in prompt
    assert "tracks" in prompt
    assert "challenger" in prompt


def test_reviewer_prompt_does_not_require_a_step_smaller_than_the_obstacle():
    """Choosing the direction is the reviewer's job; sizing the task is the orchestrator's.

    A route that is as hard as the obstacle was being self-censored as "not a bounded
    step", so the prompt has to say plainly that it is admissible and that a first
    checkable artifact is an acceptable brief.
    """
    prompt = reviewer_prompt(worker_results=[], frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}})
    assert "research tracks" in prompt
    assert "not worker tasks" in prompt
    assert "orchestrator selects" in prompt
    # An unciteable literature result is usable evidence about where a route leads.
    assert "literature" in prompt
    assert "considered_but_deferred" in prompt
    assert "portfolio retrospective" in prompt
    assert "all plan/track executions" in prompt
    assert "Every reviewer invocation is a portfolio review cycle" in prompt
    assert "TECHNIQUE_EXPLORATION" in prompt


def test_strategy_survives_any_heading_depth():
    """Heading depth must not decide whether a plan survives.

    A real reviewer call emitted `##` where the parser demanded `###` verbatim, and its
    entire strategy — 542s and roughly 200k tokens of work — was discarded. The depth a
    model happens to choose carries no meaning; the fenced JSON body is the contract.
    """
    body = """```json
{
  "research_strategy": "The gap sub-route is refuted by a verified corner witness, so pivot to the exact-variance contrapositive.",
  "next_step": {
    "kind": "TARGET_NODE",
    "difficulty_id": "full-class-cubic-bound",
    "method_id": "contradiction",
    "route_label": "exact-variance-contrapositive",
    "brief": "Assume the third moment exceeds two and exhibit an interval whose exact variance exceeds one."
  }
}
```"""
    for heading in ("## Research Strategy JSON", "### Research Strategy JSON", "#### Research Strategy JSON"):
        parsed = parse_recommendation(f"{heading}\n{body}")
        assert parsed is not None, heading
        assert parsed["next_step"]["difficulty_id"] == "full-class-cubic-bound"
        assert parsed["next_step"]["route_label"] == "exact-variance-contrapositive"


def test_strategy_prefers_the_final_heading_when_the_template_is_quoted():
    """Reviewer reasoning often restates the template before emitting the real block."""
    parsed = parse_recommendation("""### Research Strategy JSON
(I will fill this in below.)

### Research Strategy JSON
```json
{
  "research_strategy": "Pivot to the exact-variance contrapositive on the critical-path leaf.",
  "next_step": {
    "kind": "NEW_DIRECTION",
    "difficulty_id": "",
    "method_id": "direct_proof",
    "route_label": "Sharp John-Nirenberg Reconstruction",
    "brief": "Produce the closed form of the non-separable Bellman function and verify its boundary values."
  }
}
```""")
    assert parsed is not None
    assert parsed["next_step"]["kind"] == "NEW_DIRECTION"
    # Self-named labels are slugified so one route spells identically across calls.
    assert parsed["next_step"]["route_label"] == "sharp-john-nirenberg-reconstruction"


def test_research_plan_accepts_primary_and_independent_challenger_tracks():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Close the remaining cubic-bound gap.",
    "strategy": "Exploit exact variance while preserving one independent falsification challenger.",
    "tracks": [
      {
        "track_id": "exact-variance",
        "priority": "primary",
        "kind": "TARGET_NODE",
        "difficulty_id": "cubic-leaf",
        "method_id": "contradiction",
        "route_label": "exact-variance-contrapositive",
        "research_goal": "Determine whether full interval variance forces the cubic bound.",
        "rationale": "The route uses information absent from the refuted gap relaxation.",
        "avoid": "Do not reduce variance to a two-point gap."
      },
      {
        "track_id": "counterexample",
        "priority": "challenger",
        "kind": "NEW_DIRECTION",
        "difficulty_id": "",
        "method_id": "falsification",
        "route_label": "full-interval-counterexample",
        "research_goal": "Test whether an admissible finite witness can violate the cubic bound.",
        "rationale": "A witness would decisively invalidate the primary target.",
        "avoid": "Do not reuse prefix-only or dyadic relaxations."
      }
    ]
  }
}
```""")

    assert parsed is not None
    tracks = parsed["research_plan"]["tracks"]
    assert [track["track_id"] for track in tracks] == ["exact-variance", "counterexample"]
    assert "next_step" not in parsed
    assert tracks[1]["priority"] == "challenger"


def test_research_plan_preserves_route_contract_and_milestones():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Resolve the terminal obligation.",
    "strategy": "Use a route contract so planning intent remains distinct from worker execution.",
    "tracks": [{
      "track_id": "composition",
      "priority": "primary",
      "kind": "NEW_DIRECTION",
      "selection_scope": "TECHNIQUE_EXPLORATION",
      "difficulty_id": "",
      "terminal_obligation": "Resolve the terminal problem.",
      "method_id": "construction",
      "route_label": "cross-cell-composition",
      "research_goal": "Determine whether a non-separable composition can encode the target predicate.",
      "route_contract": {
        "hypothesis": "A cross-cell coupling can preserve selection while making the objective non-separable.",
        "required_invariants": ["Selection remains clean.", "The parameter bound is preserved."],
        "success_condition": "A threshold-preserving composition is constructed.",
        "failure_condition": "A structural obstruction rules out this coupling family."
      },
      "milestones": [{
        "milestone_id": "coupling-census",
        "objective": "Decide whether the smallest coupled geometry preserves clean selection.",
        "evidence_needed": "An exhaustive classification or a structural proof."
      }],
      "rationale": "This tests the route's first discriminating condition.",
      "avoid": "Do not treat a local coupling as a completed composition."
    }]
  }
}
```""")

    assert parsed is not None
    track = parsed["research_plan"]["tracks"][0]
    assert track["route_contract"]["hypothesis"].startswith("A cross-cell")
    assert track["route_contract"]["required_invariants"] == ["Selection remains clean.", "The parameter bound is preserved."]
    assert track["milestones"] == [{
        "milestone_id": "coupling-census",
        "objective": "Decide whether the smallest coupled geometry preserves clean selection.",
        "evidence_needed": "An exhaustive classification or a structural proof.",
    }]
    # The reviewer omitted the terminal-sufficiency comparison. That is a real defect, but
    # discarding the whole plan here would burn an entire reviewer cycle and hide the
    # reason; the omission must stay visible for the next reviewer cycle's retrospective instead.
    assert track["route_contract"]["terminal_sufficiency_stated"] is False


def test_route_contract_marks_a_stated_terminal_sufficiency_comparison():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Resolve the terminal obligation.",
    "strategy": "Record whether the route's best case actually entails the terminal claim.",
    "tracks": [{
      "track_id": "composition",
      "priority": "primary",
      "kind": "NEW_DIRECTION",
      "selection_scope": "TECHNIQUE_EXPLORATION",
      "difficulty_id": "",
      "terminal_obligation": "Resolve the terminal problem.",
      "method_id": "construction",
      "route_label": "cross-cell-composition",
      "research_goal": "Determine whether a non-separable composition can encode the target predicate.",
      "route_contract": {
        "hypothesis": "A cross-cell coupling preserves selection while making the objective non-separable.",
        "required_invariants": ["Selection remains clean."],
        "success_condition": "A threshold-preserving composition is constructed.",
        "failure_condition": "A structural obstruction rules out this coupling family.",
        "terminal_sufficiency": "This route's best case gives hardness only for the restricted family, which is strictly weaker than the terminal claim."
      },
      "milestones": [{
        "milestone_id": "coupling-census",
        "objective": "Decide whether the smallest coupled geometry preserves clean selection.",
        "evidence_needed": "An exhaustive classification or a structural proof."
      }],
      "rationale": "This tests the route's first discriminating condition.",
      "avoid": "Do not treat a local coupling as a completed composition."
    }]
  }
}
```""")

    assert parsed is not None
    contract = parsed["research_plan"]["tracks"][0]["route_contract"]
    assert contract["terminal_sufficiency_stated"] is True
    assert contract["terminal_sufficiency"].startswith("This route's best case")
    # This plan predates falsification_condition; the omission must stay visible rather
    # than silently defaulting to "already tested".
    assert contract["falsification_condition_stated"] is False


def test_route_contract_marks_a_stated_falsification_condition():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Resolve the terminal obligation.",
    "strategy": "Record the converse/soundness-facing test that would falsify the route.",
    "tracks": [{
      "track_id": "composition",
      "priority": "primary",
      "kind": "NEW_DIRECTION",
      "selection_scope": "TECHNIQUE_EXPLORATION",
      "difficulty_id": "",
      "terminal_obligation": "Resolve the terminal problem.",
      "method_id": "construction",
      "route_label": "cross-cell-composition",
      "research_goal": "Determine whether a non-separable composition can encode the target predicate.",
      "route_contract": {
        "hypothesis": "A cross-cell coupling preserves selection while making the objective non-separable.",
        "required_invariants": ["Selection remains clean."],
        "success_condition": "A threshold-preserving composition is constructed.",
        "failure_condition": "A structural obstruction rules out this coupling family.",
        "terminal_sufficiency": "This route's best case entails the terminal claim if soundness holds.",
        "falsification_condition": "An arbitrary final matching that achieves threshold score without corresponding to a clique would falsify the mechanism."
      },
      "milestones": [{
        "milestone_id": "coupling-census",
        "objective": "Decide whether the smallest coupled geometry preserves clean selection.",
        "evidence_needed": "An exhaustive classification or a structural proof."
      }],
      "rationale": "This tests the route's first discriminating condition.",
      "avoid": "Do not treat a local coupling as a completed composition."
    }]
  }
}
```""")

    assert parsed is not None
    contract = parsed["research_plan"]["tracks"][0]["route_contract"]
    assert contract["falsification_condition_stated"] is True
    assert contract["falsification_condition"].startswith("An arbitrary final matching")


def test_reviewer_prompt_requires_top_down_obligation_decomposition():
    prompt = reviewer_prompt(worker_results=[], frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}})
    assert "Decompose the terminal obligation top-down" in prompt
    assert "converse/soundness direction" in prompt
    assert "falsification_condition" in prompt
    assert "SCAFFOLDING_ASSUMPTION_UNVERIFIED" in prompt


def test_research_plan_rejects_track_without_route_identity():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Close the gap.",
    "strategy": "Try an alternative.",
    "tracks": [{
      "track_id": "missing-route",
      "priority": "primary",
      "kind": "TARGET_NODE",
      "difficulty_id": "leaf",
      "method_id": "contradiction",
      "research_goal": "Test the target.",
      "rationale": "No route identifier was given."
    }]
  }
}
```""")

    assert parsed is None


def test_research_plan_rejects_new_direction_bound_to_a_canonical_node():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Test an independent route.",
    "strategy": "A graph-external challenger must not silently reuse the old node.",
    "tracks": [{
      "track_id": "mislabelled-new-route",
      "priority": "primary",
      "kind": "NEW_DIRECTION",
      "difficulty_id": "old-leaf",
      "method_id": "construction",
      "route_label": "new-name-old-node",
      "research_goal": "Test an independent construction.",
      "rationale": "It is allegedly independent.",
      "avoid": "Avoid the old proof."
    }]
  }
}
```""")

    assert parsed is None


def test_deferred_directions_are_retained_and_bounded():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_strategy": "Attack the leaf by the exact-variance contrapositive.",
  "next_step": {
    "kind": "TARGET_NODE",
    "difficulty_id": "leaf",
    "method_id": "contradiction",
    "route_label": "exact-variance",
    "brief": "Exhibit an interval whose exact variance exceeds one."
  },
  "considered_but_deferred": [
    {"direction": "Full-class sharp John-Nirenberg exponential bound", "reason": "Equivalent difficulty to the obstacle; revisit once a partial certificate exists."},
    {"direction": "", "reason": "dropped because it names no direction"},
    {"direction": "names no reason", "reason": ""}
  ]
}
```""")
    assert parsed is not None
    deferred = parsed["considered_but_deferred"]
    assert len(deferred) == 1
    assert "John-Nirenberg" in deferred[0]["direction"]
    assert "Equivalent difficulty" in deferred[0]["reason"]


def test_reviewer_plan_records_selection_level_and_hard_technique_tabu():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Leave a refuted separable certificate and reconstruct the full Bellman geometry.",
    "strategy": "The verified split counterexample is a hard tabu for the separable candidate; use a technique-level geometric route with an explicit reopen condition.",
    "tabu_rules": [{
      "tabu_id": "separable-bellman",
      "level": "hard",
      "route_label": "separable-bellman-v-only",
      "mechanism": "A verified realizable split contradicts the one-variable separable certificate.",
      "applies_to": "ALL",
      "evidence_refs": ["verified_propositions/counterexample.md"],
      "reopen_condition": "A new candidate must change the state space or splitting premise."
    }],
    "tracks": [{
      "track_id": "ma-foliation",
      "priority": "primary",
      "kind": "NEW_DIRECTION",
      "selection_scope": "TECHNIQUE_EXPLORATION",
      "difficulty_id": "",
      "terminal_obligation": "Prove the sharp full-class cubic bound.",
      "method_id": "direct_proof",
      "route_label": "nonseparable-ma-foliation",
      "tabu_rule_ids": ["separable-bellman"],
      "reopen_condition": "Park if no candidate characteristic family survives a finite split check.",
      "research_goal": "Construct or falsify a nonseparable developable Bellman foliation.",
      "rationale": "It changes the state geometry rather than renaming the refuted ansatz.",
      "avoid": "Do not use a v-only separable certificate."
    }]
  }
}
```""")

    assert parsed is not None
    plan = parsed["research_plan"]
    assert plan["tabu_rules"][0]["level"] == "hard"
    assert plan["tracks"][0]["selection_scope"] == "TECHNIQUE_EXPLORATION"
    assert plan["tracks"][0]["tabu_rule_ids"] == ["separable-bellman"]


def test_reviewer_plan_rejects_reselecting_its_own_hard_tabu_route():
    parsed = parse_recommendation("""### Research Strategy JSON
```json
{"research_plan":{"objective":"Do not repeat a refuted route.","strategy":"The refuted premise remains hard tabu.","tabu_rules":[{"tabu_id":"refuted-route","level":"hard","route_label":"refuted-route","mechanism":"A verified witness refutes its premise.","applies_to":"ALL","evidence_refs":["verified_propositions/witness.md"],"reopen_condition":"New evidence changes the premise."}],"tracks":[{"track_id":"retry","priority":"primary","kind":"NEW_DIRECTION","selection_scope":"TECHNIQUE_EXPLORATION","difficulty_id":"","terminal_obligation":"Resolve the original problem.","method_id":"direct_proof","route_label":"refuted-route","tabu_rule_ids":["refuted-route"],"research_goal":"Retry the refuted route.","rationale":"This is deliberately inconsistent.","avoid":"None."}]}}
```
""")

    assert parsed is None


def test_global_synthesis_requires_explicit_consolidation_contract():
    invalid = parse_recommendation("""### Research Strategy JSON
```json
{"research_plan":{"objective":"Synthesize.","strategy":"Combine evidence.","tracks":[{"track_id":"bad","priority":"primary","kind":"NEW_DIRECTION","selection_scope":"GLOBAL_SYNTHESIS","difficulty_id":"","terminal_obligation":"Resolve the original problem.","method_id":"direct_proof","route_label":"global-synthesis","research_goal":"Combine the evidence.","rationale":"It is time.","avoid":"None."}]}}
```""")
    assert invalid is None


def _plan_body(*, priority: str, kind: str, selection_scope: str, difficulty_id: str = "leaf") -> str:
    terminal_obligation = (
        '"terminal_obligation":"Resolve the original problem.",' if selection_scope != "NODE_ROUTE" else ""
    )
    return (
        '{"research_plan":{"objective":"Continue.","strategy":"Keep going.","tracks":[{'
        f'"track_id":"t","priority":"{priority}","kind":"{kind}","selection_scope":"{selection_scope}",'
        f'"difficulty_id":"{difficulty_id if selection_scope in {"LOCAL_REPAIR", "NODE_ROUTE"} else ""}",'
        f'{terminal_obligation}'
        '"method_id":"direct_proof","route_label":"route-x","research_goal":"Continue the route.",'
        '"rationale":"Keep going.","avoid":"None."}]}}'
    )


def test_force_pivot_rejects_local_repair_primary_track():
    body = _plan_body(priority="primary", kind="TARGET_NODE", selection_scope="LOCAL_REPAIR")
    rejected = parse_recommendation(
        f"### Research Strategy JSON\n```json\n{body}\n```",
        stagnation_level="FORCE_PIVOT",
    )
    assert rejected is None
    # The same plan is admissible without the escalation.
    accepted = parse_recommendation(f"### Research Strategy JSON\n```json\n{body}\n```")
    assert accepted is not None


def test_force_pivot_rejects_node_route_primary_track():
    body = _plan_body(priority="primary", kind="TARGET_NODE", selection_scope="NODE_ROUTE")
    rejected = parse_recommendation(
        f"### Research Strategy JSON\n```json\n{body}\n```",
        stagnation_level="FORCE_PIVOT",
    )
    assert rejected is None


def test_force_pivot_rejects_global_synthesis_as_the_escape_hatch():
    body = (
        '{"research_plan":{"objective":"Attack directly.","strategy":"Combine everything.",'
        '"tracks":[{"track_id":"t","priority":"primary","kind":"NEW_DIRECTION",'
        '"selection_scope":"GLOBAL_SYNTHESIS","difficulty_id":"",'
        '"terminal_obligation":"Resolve the original problem.","method_id":"consolidation",'
        '"route_label":"global-synthesis","research_goal":"Combine the evidence.",'
        '"rationale":"It is time.","avoid":"None."}]}}'
    )
    rejected = parse_recommendation(
        f"### Research Strategy JSON\n```json\n{body}\n```",
        stagnation_level="FORCE_PIVOT",
    )
    assert rejected is None


def test_force_pivot_accepts_graph_portfolio_primary_track():
    body = (
        '{"research_plan":{"objective":"Pivot.","strategy":"Move across the graph.",'
        '"tracks":[{"track_id":"t","priority":"primary","kind":"NEW_DIRECTION",'
        '"selection_scope":"GRAPH_PORTFOLIO","difficulty_id":"",'
        '"target_difficulty_ids":["ancestor-node"],"method_id":"direct_proof",'
        '"route_label":"route-x","research_goal":"Attack the ancestor.",'
        '"rationale":"Diagnose the recurring blocker.","avoid":"None."}]}}'
    )
    accepted = parse_recommendation(
        f"### Research Strategy JSON\n```json\n{body}\n```",
        stagnation_level="FORCE_PIVOT",
    )
    assert accepted is not None
    assert accepted["research_plan"]["tracks"][0]["selection_scope"] == "GRAPH_PORTFOLIO"


def test_force_pivot_does_not_block_a_challenger_still_at_local_repair():
    """FORCE_PIVOT only constrains the primary track's scope, not supporting/challenger tracks."""
    body = (
        '{"research_plan":{"objective":"Pivot the primary, keep probing locally.",'
        '"strategy":"Move the primary across the graph while a challenger keeps testing the local repair.",'
        '"tracks":['
        '{"track_id":"primary-pivot","priority":"primary","kind":"NEW_DIRECTION",'
        '"selection_scope":"TECHNIQUE_EXPLORATION","difficulty_id":"",'
        '"terminal_obligation":"Resolve the original problem.","method_id":"direct_proof",'
        '"route_label":"route-new","research_goal":"Try a new framework.",'
        '"rationale":"Diagnose the recurring blocker.","avoid":"None."},'
        '{"track_id":"challenger-local","priority":"challenger","kind":"TARGET_NODE",'
        '"selection_scope":"LOCAL_REPAIR","difficulty_id":"leaf",'
        '"method_id":"direct_proof","route_label":"route-x","research_goal":"One more bounded probe.",'
        '"rationale":"Cheap to test.","avoid":"None."}'
        ']}}'
    )
    accepted = parse_recommendation(
        f"### Research Strategy JSON\n```json\n{body}\n```",
        stagnation_level="FORCE_PIVOT",
    )
    assert accepted is not None


def test_force_pivot_allows_a_hold_plan():
    """A HOLD (no tracks) is a legitimate response to a forced pivot, not a bypass."""
    held = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_plan": {
    "objective": "Pause and re-plan.",
    "strategy": "No evidence-backed route leaves the stalled mechanism yet.",
    "tracks": [],
    "hold_reason": "Diagnosing the repeated blocker before proposing a portfolio move."
  }
}
```""", stagnation_level="FORCE_PIVOT")
    assert held is not None
    assert held["research_plan"]["tracks"] == []
