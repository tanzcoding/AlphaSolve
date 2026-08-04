"""search.proxy 单元测试（§3 廉价分级信号，独立于已删除的 selection/duel 机制）。"""
from __future__ import annotations

from alphasolve.solver.search import (
    InsightRecord,
    ProgressSample,
    ProxyScore,
    SearchGraph,
    compute_proxy,
    progress_rate,
    rank_by_proxy,
)


def test_compute_proxy_uses_first_order_delta():
    g = SearchGraph()
    n = g.add_node("x")
    prev = InsightRecord(verified_implications=["a"])
    n.insight = InsightRecord(
        verified_implications=["a", "b", "c"],
        open_subgoals=["g1"],
        falsified_implications=[],
    )
    score = compute_proxy(n, prev_insight=prev)
    assert score.verified_implications == 3
    assert score.verified_delta == 2  # 3 - 1
    assert score.open_subgoals == 1


def test_proxy_ranking_direction():
    # 少 open_subgoals / 多 verified_delta / 少 falsif → 更优先
    good = ProxyScore(open_subgoals=1, verified_implications=5, verified_delta=3, falsification_hits=0)
    bad = ProxyScore(open_subgoals=4, verified_implications=5, verified_delta=1, falsification_hits=2)
    assert good.ranking_key() < bad.ranking_key()
    assert rank_by_proxy([("bad", bad), ("good", good)]) == ["good", "bad"]


def test_progress_rate():
    samples = [ProgressSample(verified_delta=2, dispatches=2), ProgressSample(verified_delta=1, dispatches=2)]
    assert progress_rate(samples) == 3 / 4
    assert progress_rate([]) == 0.0
