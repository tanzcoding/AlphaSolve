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


def test_plan_accepts_evidence_backed_graph_observation():
    plan = parse_recommendation("""### Planning Recommendation JSON
```json
{
  "action": "DISPATCH_NEW_DIRECTION",
  "difficulty_id": "",
  "dispatch_mode": "",
  "method_id": "computation",
  "reason": "The dual route is independent.",
  "exploration_brief": "Test a bounded dual certificate.",
  "graph_observations": [{
    "kind": "EDGE_SUSPECT",
    "target_ids": ["root", "leaf"],
    "summary": "The verified counterexample contradicts the claimed prerequisite relation.",
    "evidence_refs": ["verified_propositions/counterexample.md"],
    "recommended_graph_effect": "reconsider_edge"
  }]
}
```""")
    assert plan is not None
    assert plan["graph_observations"][0]["kind"] == "EDGE_SUSPECT"


def test_reviewer_prompt_explains_source_weights_and_observations():
    prompt = reviewer_prompt(worker_results=[], frontier={"frontier_revision": "r", "dispatchable": [], "graph": {}, "sources": {}})
    assert "verified propositions" in prompt
    assert "knowledge is a navigation aid" in prompt
    assert "graph_observations" in prompt
