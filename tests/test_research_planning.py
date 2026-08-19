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


def test_reviewer_prompt_uses_attempt_statistics_and_parent_attack_guidance():
    prompt = reviewer_prompt(worker_results=[], frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}})
    assert "verified propositions" in prompt
    assert "knowledge is a navigation aid" in prompt
    assert "(node, method)" in prompt
    assert "parent or ancestor" in prompt
    assert "next mathematical direction freely" in prompt
    assert "research_strategy" in prompt
    assert "next_step" in prompt
