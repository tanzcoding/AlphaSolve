"""search.insight 单元测试（§6 result/insight 分离 + backprop + falsification）。"""
from __future__ import annotations

from alphasolve.solver.search import (
    Delta,
    FalsifiedImplication,
    InsightRecord,
    SearchGraph,
    apply_falsification,
    backpropagate_insights,
)
from alphasolve.solver.search.graph import NodeStatus


def test_insight_yaml_round_trip_preserves_unicode_and_order():
    rec = InsightRecord(
        verified_implications=["X ⇒ Y₁"],
        falsified_implications=[FalsifiedImplication("X ⇏ Z", "counterexample n=3")],
        open_subgoals=["prove W", "bound X"],
        direction_summary="push via W",
    )
    text = rec.to_yaml()
    back = InsightRecord.from_yaml(text)
    assert back.verified_implications == ["X ⇒ Y₁"]
    assert back.falsified_implications[0].implication == "X ⇏ Z"
    assert back.falsified_implications[0].counterexample == "counterexample n=3"
    assert back.open_subgoals == ["prove W", "bound X"]
    assert back.open_subgoal_count() == 2


def _merge_abstractor(children, max_words):
    verified: list[str] = []
    subgoals: list[str] = []
    for c in children:
        for v in c.verified_implications:
            if v not in verified:
                verified.append(v)
        for s in c.open_subgoals:
            if s not in subgoals:
                subgoals.append(s)
    return InsightRecord(
        verified_implications=verified,
        open_subgoals=subgoals,
        direction_summary=" ".join(f"({c.direction_summary})" for c in children)[: max_words * 8],
    )


def test_backpropagate_insights_refreshes_layer_by_layer():
    g = SearchGraph()
    root = g.add_node("root")
    mid = g.add_node("mid", parents=[root.state_id])
    leaf = g.add_node("leaf", parents=[mid.state_id])
    sib = g.add_node("sib", parents=[mid.state_id])
    leaf.insight = InsightRecord(verified_implications=["A⇒B"], open_subgoals=["g1"])
    sib.insight = InsightRecord(verified_implications=["A⇒C"], open_subgoals=["g2"])

    updated = backpropagate_insights(g, leaf.state_id, _merge_abstractor)
    updated_ids = {n.state_id for n in updated}
    assert mid.state_id in updated_ids
    assert root.state_id in updated_ids
    # mid 聚合其直接 children（leaf + sib）
    assert set(mid.insight.verified_implications) == {"A⇒B", "A⇒C"}
    assert set(mid.insight.open_subgoals) == {"g1", "g2"}
    # root 聚合 mid（逐层向上，mid 已先被刷新）
    assert set(root.insight.verified_implications) == {"A⇒B", "A⇒C"}


def test_apply_falsification_hard_prunes_dependent_nodes():
    g = SearchGraph()
    root = g.add_node("assume ansatz B works")
    dep = g.add_node("build further", parents=[root.state_id])
    dep.insight = InsightRecord(open_subgoals=["need ansatz B based estimate"])
    other = g.add_node("unrelated route C", parents=[root.state_id])

    pruned = apply_falsification(g, "ansatz B")
    pruned_ids = {n.state_id for n in pruned}
    assert root.state_id in pruned_ids  # hypothesis 含 "ansatz B"
    assert dep.state_id in pruned_ids  # insight open_subgoal 引用了 "ansatz B"
    assert other.state_id not in pruned_ids  # 不相关路线不误伤
    assert root.status == NodeStatus.PRUNED
    assert root.prune_reason


def test_apply_falsification_empty_ansatz_prunes_nothing():
    g = SearchGraph()
    g.add_node("something")
    assert apply_falsification(g, "") == []
