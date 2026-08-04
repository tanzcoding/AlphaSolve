"""search.graph 单元测试（§2 数据模型：DAG + delta 视图聚合）。"""
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
