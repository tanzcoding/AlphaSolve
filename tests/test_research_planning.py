from __future__ import annotations

from alphasolve.solver.research_planning import frontier_projection, parse_recommendation, reviewer_prompt


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
