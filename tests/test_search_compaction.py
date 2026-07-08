"""search.compaction 单元测试（§7 分层压缩 + 公共祖先增量组装）。"""
from __future__ import annotations

from alphasolve.solver.search import (
    CompactionBudget,
    Delta,
    SearchGraph,
    assemble_critic_view,
    build_worker_context,
    trim_ancestor_insights,
    trim_vp_index,
)


def test_assemble_critic_view_factors_out_common_ancestor():
    g = SearchGraph()
    root = g.add_node("root", delta=Delta(proof_ref="p0"))
    parent = g.add_node("parent", parents=[root.state_id], delta=Delta(proof_ref="p1"))
    a = g.add_node("a", parents=[parent.state_id], delta=Delta(proof_ref="pa"))
    b = g.add_node("b", parents=[parent.state_id], delta=Delta(proof_ref="pb"))

    view = assemble_critic_view(g, a.state_id, b.state_id)
    assert view.common_ancestor_id == parent.state_id
    assert view.common_view_refs == ["p0", "p1"]  # 公共部分只出现一次
    assert view.a_delta_refs == ["pa"]
    assert view.b_delta_refs == ["pb"]


def test_trim_ancestor_insights_keeps_nearest_drops_farthest():
    near_to_far = [f"layer{i}" for i in range(12)]
    kept = trim_ancestor_insights(near_to_far, max_layers=8)
    assert kept == [f"layer{i}" for i in range(8)]  # 保近端，丢最远
    assert trim_ancestor_insights(near_to_far, max_layers=0) == []


def test_trim_vp_index_caps_count_without_altering_content():
    refs = [f"prop{i}" for i in range(30)]
    kept = trim_vp_index(refs, max_items=20)
    assert kept == refs[:20]
    assert all(k in refs for k in kept)  # 内容不改写


class _FakeSummarizer:
    def summarize(self, trajectory_text, *, max_words=300):
        return f"SUMMARY[{max_words}]: " + trajectory_text.split("\n")[0]


def test_build_worker_context_is_layered():
    g = SearchGraph()
    root = g.add_node("root", delta=Delta(proof_ref="p0"))
    leaf = g.add_node("leaf", parents=[root.state_id], delta=Delta(proof_ref="p1"))
    ctx = build_worker_context(
        g,
        leaf.state_id,
        ancestor_insight_summaries_near_to_far=["near", "far"],
        trajectory_text="tried X and failed\nmore detail",
        summarizer=_FakeSummarizer(),
        budget=CompactionBudget(max_ancestor_insight_layers=1, max_vp_index=20),
    )
    # 无损层：权威 prop 引用逐字保留
    assert ctx.authoritative_refs == ["p0", "p1"]
    # 有损层：ancestor insight 被裁到近 1 层 + 轨迹被摘要
    assert "near" in ctx.trajectory_summary
    assert "far" not in ctx.trajectory_summary
    assert "SUMMARY[" in ctx.trajectory_summary
