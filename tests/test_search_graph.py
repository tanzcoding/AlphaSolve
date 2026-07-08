"""search.graph 单元测试（§2 数据模型 + §5.4 visit_count 全祖先回传）。"""
from __future__ import annotations

from alphasolve.solver.search import Delta, NodeStatus, SearchGraph


def _chain() -> tuple[SearchGraph, str, str, str]:
    g = SearchGraph()
    root = g.add_node("root goal", delta=Delta(proof_ref="p0"))
    a = g.add_node("mid", parents=[root.state_id], delta=Delta(proof_ref="p1"))
    leaf = g.add_node("leaf", parents=[a.state_id], delta=Delta(proof_ref="p2"))
    return g, root.state_id, a.state_id, leaf.state_id


def test_add_node_sets_depth_and_edges():
    g, root, a, leaf = _chain()
    assert g.get(root).depth == 0
    assert g.get(a).depth == 1
    assert g.get(leaf).depth == 2
    assert leaf in g.get(a).children
    assert g.get(leaf).parents == [a]


def test_unique_uuid_and_unknown_parent():
    g = SearchGraph()
    n1 = g.add_node("x")
    n2 = g.add_node("y")
    assert n1.state_id != n2.state_id
    try:
        g.add_node("z", parents=["does-not-exist"])
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_materialize_view_accumulates_deltas_root_to_self():
    g, root, a, leaf = _chain()
    assert g.materialize_view(leaf) == ["p0", "p1", "p2"]
    assert g.materialize_view(a) == ["p0", "p1"]
    assert g.materialize_view(root) == ["p0"]


def test_ancestors_dedup_in_dag():
    # crossover: leaf 有两个父，两父共享同一 root → root 只应出现一次
    g = SearchGraph()
    root = g.add_node("root")
    p = g.add_node("p", parents=[root.state_id])
    q = g.add_node("q", parents=[root.state_id])
    leaf = g.add_node("merged", parents=[p.state_id, q.state_id])
    anc_ids = [n.state_id for n in g.ancestors(leaf.state_id)]
    assert sorted(anc_ids) == sorted([root.state_id, p.state_id, q.state_id])
    assert anc_ids.count(root.state_id) == 1


def test_common_ancestor():
    g = SearchGraph()
    root = g.add_node("root")
    a = g.add_node("a", parents=[root.state_id])
    x = g.add_node("x", parents=[a.state_id])
    y = g.add_node("y", parents=[a.state_id])
    ca = g.common_ancestor(x.state_id, y.state_id)
    assert ca is not None and ca.state_id == a.state_id


def test_on_dispatch_backpropagates_visit_to_all_ancestors():
    g, root, a, leaf = _chain()
    g.on_dispatch(leaf)
    assert g.get(leaf).visit_count == 1
    assert g.get(a).visit_count == 1
    assert g.get(root).visit_count == 1
    assert g.get(leaf).self_picked_count == 1
    assert g.get(a).self_picked_count == 0
    assert g.total_visits == 1


def test_explore_bonus_decreases_when_branch_is_mined():
    g, root, a, leaf = _chain()
    before = g.explore_bonus(leaf)
    for _ in range(5):
        g.on_dispatch(leaf)
    after = g.explore_bonus(leaf)
    assert after < before  # 被深挖的支探索偏置下降


def test_q_value_neutral_when_no_duels():
    g = SearchGraph()
    n = g.add_node("x")
    assert n.q_value == 0.5  # 没比过是中性 0.5，不是 0


def test_prune_marks_but_keeps_node():
    g, root, a, leaf = _chain()
    g.prune(leaf, "dead end")
    assert g.get(leaf).status == NodeStatus.PRUNED
    assert g.get(leaf).prune_reason == "dead end"
    assert leaf in g  # 节点不删除
    try:
        g.prune(a, "")
        assert False, "empty reason must raise"
    except ValueError:
        pass
