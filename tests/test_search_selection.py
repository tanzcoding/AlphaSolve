"""search.selection + search.proxy 单元测试（§3 / §5）。"""
from __future__ import annotations

from alphasolve.solver.search import (
    Delta,
    DuelLedger,
    DuelOutcome,
    InsightRecord,
    ProgressSample,
    ProxyScore,
    Quota,
    SearchGraph,
    allocate_quota,
    anneal_exploit_fraction,
    compute_proxy,
    jaccard,
    progress_rate,
    rank_by_proxy,
    record_duel,
    select_diverse_fill,
    tie_break,
)


# ---- proxy (§3) -------------------------------------------------------


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


# ---- 平局定案 (§5.3) --------------------------------------------------


def _two_siblings() -> tuple[SearchGraph, str, str]:
    g = SearchGraph()
    root = g.add_node("root")
    a = g.add_node("a", parents=[root.state_id])
    b = g.add_node("b", parents=[root.state_id])
    return g, a.state_id, b.state_id


def test_decisive_win_scores_one():
    g, a, b = _two_siblings()
    eff = record_duel(g, a, b, DuelOutcome.A_WINS)
    assert eff.scored and not eff.crossover_candidate
    assert g.get(a).wins == 1.0 and g.get(a).duels == 1
    assert g.get(b).wins == 0.0 and g.get(b).duels == 1


def test_comparable_tie_scores_half_each_and_flags_crossover():
    g, a, b = _two_siblings()
    eff = record_duel(g, a, b, DuelOutcome.COMPARABLE_TIE)
    assert eff.scored and eff.crossover_candidate
    assert g.get(a).wins == 0.5 and g.get(b).wins == 0.5
    assert g.get(a).duels == 1 and g.get(b).duels == 1
    assert g.get(a).crossover_candidate and g.get(b).crossover_candidate
    # +0.5 让 Q 无偏地指向 0.5
    assert g.get(a).q_value == 0.5


def test_incomparable_does_not_touch_pool():
    g, a, b = _two_siblings()
    eff = record_duel(g, a, b, DuelOutcome.INCOMPARABLE)
    assert not eff.scored and eff.diversity_protected
    assert g.get(a).wins == 0.0 and g.get(a).duels == 0
    assert g.get(b).wins == 0.0 and g.get(b).duels == 0
    assert not g.get(a).crossover_candidate


# ---- 反复平局止损 (§5.6) ----------------------------------------------


def test_duel_ledger_stops_after_k():
    ledger = DuelLedger(max_duels_per_pair=2)
    g, a, b = _two_siblings()
    assert not ledger.exhausted(a, b)
    record_duel(g, a, b, DuelOutcome.COMPARABLE_TIE, ledger=ledger)
    assert not ledger.exhausted(a, b)
    record_duel(g, a, b, DuelOutcome.COMPARABLE_TIE, ledger=ledger)
    assert ledger.exhausted(a, b)  # 比够 K 次，停止比较
    # 无序对：换顺序计数一致
    assert ledger.count(b, a) == 2


# ---- tie-break 优先级 (§5.3) ------------------------------------------


def test_tie_break_prefers_proxy_then_visit_then_novelty():
    g, a, b = _two_siblings()
    # proxy 分不同：a 更优先
    proxy = {
        a: ProxyScore(open_subgoals=1, verified_implications=1, verified_delta=1, falsification_hits=0),
        b: ProxyScore(open_subgoals=3, verified_implications=1, verified_delta=1, falsification_hits=0),
    }
    assert tie_break(g, a, b, proxy=proxy) == a

    # proxy 相同 → 比 visit_count，少者优先
    same = ProxyScore(open_subgoals=2, verified_implications=1, verified_delta=1, falsification_hits=0)
    proxy2 = {a: same, b: same}
    g.get(a).visit_count = 5
    g.get(b).visit_count = 1
    assert tie_break(g, a, b, proxy=proxy2) == b

    # proxy + visit 都相同 → novelty 高者优先
    g.get(a).visit_count = 1
    assert tie_break(g, a, b, proxy=proxy2, novelty=lambda sid: 1.0 if sid == a else 0.0) == a


# ---- 配额 + 退火 (§5.5) -----------------------------------------------


def test_anneal_exploit_fraction_monotone_bounds():
    lo = anneal_exploit_fraction(0.0, min_fraction=0.3, max_fraction=0.8)
    hi = anneal_exploit_fraction(1000.0, min_fraction=0.3, max_fraction=0.8)
    mid = anneal_exploit_fraction(1.0, min_fraction=0.3, max_fraction=0.8)
    assert lo == 0.3
    assert 0.3 < mid < 0.8
    assert hi > mid


def test_allocate_quota_respects_budget_and_flags():
    q = allocate_quota(10, progress_rate=5.0, has_siblings=True, cycle=5, depth1_threshold=3)
    assert isinstance(q, Quota)
    assert q.total <= 10
    assert q.exploit >= 1
    assert q.sibling_explore >= 1  # 有兄弟
    assert q.depth1_explore == 1  # cycle >= threshold

    # cycle 未达阈值 → 不强开新 arm；无兄弟 → 无 sibling_explore
    q2 = allocate_quota(4, progress_rate=0.0, has_siblings=False, cycle=1, depth1_threshold=3)
    assert q2.depth1_explore == 0
    assert q2.sibling_explore == 0
    assert q2.total <= 4

    # 零预算不派
    assert allocate_quota(0, progress_rate=1.0, has_siblings=True, cycle=9).total == 0


# ---- fill 多样性 (§5.5 E) ---------------------------------------------


def test_jaccard():
    assert jaccard(["a", "b"], ["a", "b"]) == 1.0
    assert jaccard(["a"], ["b"]) == 0.0
    assert jaccard([], []) == 0.0


def test_select_diverse_fill_skips_near_duplicates_and_underfills():
    g = SearchGraph()
    root = g.add_node("root", delta=Delta(proof_ref="p0"))
    # a、b 视图几乎一样（都只加 p0 之上一个相同引用）→ 高相似，应只选一个
    a = g.add_node("a", parents=[root.state_id], delta=Delta(proof_ref="p1"))
    b = g.add_node("b", parents=[root.state_id], delta=Delta(proof_ref="p1"))
    c = g.add_node("c", parents=[root.state_id], delta=Delta(proof_ref="p2"))
    proxy = {
        a.state_id: ProxyScore(1, 1, 1, 0),
        b.state_id: ProxyScore(1, 1, 1, 0),
        c.state_id: ProxyScore(2, 1, 1, 0),
    }
    picked = select_diverse_fill(
        g, [a.state_id, b.state_id, c.state_id], proxy=proxy, k=3, max_similarity=0.8
    )
    # a 与 b 视图相同(=[p0,p1]) 被去重；最终应是 a 和 c，少派不凑数（不会硬塞 b）
    assert a.state_id in picked
    assert c.state_id in picked
    assert b.state_id not in picked
    assert len(picked) == 2
