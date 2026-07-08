"""SearchSession（调度层编排门面）单元测试。

覆盖 worker 生命周期映射（on_spawn / on_worker_result）、proxy 排序、兄弟 duel
候选与止损、四值 duel 记账、配额建议、falsification 转发、以及 advise() 打包与
cycle 推进。纯算法，无 LLM、无文件系统依赖。
"""
from __future__ import annotations

from alphasolve.solver.search import (
    DuelOutcome,
    NodeStatus,
    SearchSession,
)


def _spawn_and_finish(session, worker_id, hint, *, status, summary, verified_file=None, proposition_file=None, solved=False):
    node = session.on_spawn(worker_id, hint)
    payload = {
        "worker_id": worker_id,
        "status": status,
        "summary": summary,
        "solved_problem": solved,
    }
    if verified_file:
        payload["verified_file"] = verified_file
    if proposition_file:
        payload["proposition_file"] = proposition_file
    session.on_worker_result(payload)
    return node


def test_on_spawn_creates_depth1_sibling_and_records_dispatch():
    s = SearchSession()
    n = s.on_spawn("w1", "try route A")
    assert n.depth == 1
    assert s.root.state_id in n.parents
    assert n.status == NodeStatus.RUNNING
    # dispatch 回传：自身 + root visit_count 各 +1（§5.4）
    assert n.visit_count == 1
    assert s.root.visit_count == 1
    assert s.graph.total_visits == 1


def test_on_spawn_is_idempotent_per_worker():
    s = SearchSession()
    a = s.on_spawn("w1", "h")
    b = s.on_spawn("w1", "h-again")
    assert a.state_id == b.state_id
    assert s.graph.total_visits == 1  # 不重复 dispatch


def test_on_worker_result_verified_builds_delta_and_insight():
    s = SearchSession()
    node = _spawn_and_finish(
        s, "w1", "prove lemma", status="verified",
        summary="lemma X holds", verified_file="verified_propositions/x.md",
    )
    assert node.status == NodeStatus.DONE
    assert node.delta.proof_ref == "verified_propositions/x.md"
    assert node.insight is not None
    assert node.insight.verified_implications == ["lemma X holds"]
    score = s.proxy_of(node.state_id)
    assert score.verified_implications == 1
    assert score.open_subgoals == 0


def test_failure_records_open_subgoal_but_does_not_prune():
    s = SearchSession()
    node = _spawn_and_finish(s, "w1", "risky route", status="failed", summary="stuck at step 3")
    # informative failure：记未决子目标，但不剪枝（§6.3）
    assert node.status == NodeStatus.DONE
    assert node.status != NodeStatus.PRUNED
    assert node.insight.open_subgoals == ["stuck at step 3"]
    assert s.proxy_of(node.state_id).open_subgoals == 1


def test_unknown_worker_result_returns_none():
    s = SearchSession()
    assert s.on_worker_result({"worker_id": "ghost", "status": "verified"}) is None


def test_frontier_ranking_orders_verified_before_open():
    s = SearchSession()
    _spawn_and_finish(s, "w1", "open route", status="failed", summary="two gaps remain")
    _spawn_and_finish(s, "w2", "verified route", status="verified", summary="A⇒B")
    ranking = s.frontier_ranking()
    assert len(ranking) == 2
    # verified（open_subgoals=0）应排在 open（open_subgoals>0）之前
    assert ranking[0]["hypothesis"] == "verified route"
    assert ranking[0]["open_subgoals"] == 0
    assert ranking[1]["open_subgoals"] == 1


def test_sibling_duel_candidates_and_record_updates_win_pool():
    s = SearchSession()
    a = _spawn_and_finish(s, "w1", "route A", status="verified", summary="A1")
    b = _spawn_and_finish(s, "w2", "route B", status="verified", summary="B1")
    pairs = s.sibling_duel_candidates()
    assert len(pairs) == 1
    pa, pb = pairs[0]
    assert {pa, pb} == {a.state_id, b.state_id}

    effect = s.record_sibling_duel(pa, pb, DuelOutcome.A_WINS)
    assert effect.scored is True
    winner = s.graph.get(pa)
    assert winner.wins == 1.0
    assert winner.duels == 1


def test_comparable_tie_flags_crossover_and_scores_half():
    s = SearchSession()
    a = _spawn_and_finish(s, "w1", "A", status="verified", summary="A1")
    b = _spawn_and_finish(s, "w2", "B", status="verified", summary="B1")
    effect = s.record_sibling_duel(a.state_id, b.state_id, DuelOutcome.COMPARABLE_TIE)
    assert effect.crossover_candidate is True
    assert s.graph.get(a.state_id).wins == 0.5
    assert s.graph.get(b.state_id).wins == 0.5
    assert s.graph.get(a.state_id).crossover_candidate is True


def test_incomparable_does_not_score():
    s = SearchSession()
    a = _spawn_and_finish(s, "w1", "A", status="verified", summary="A1")
    b = _spawn_and_finish(s, "w2", "B", status="verified", summary="B1")
    effect = s.record_sibling_duel(a.state_id, b.state_id, DuelOutcome.INCOMPARABLE)
    assert effect.scored is False
    assert effect.diversity_protected is True
    assert s.graph.get(a.state_id).wins == 0.0
    assert s.graph.get(a.state_id).duels == 0


def test_duel_stop_loss_exhausts_pair():
    s = SearchSession(max_duels_per_pair=2)
    a = _spawn_and_finish(s, "w1", "A", status="verified", summary="A1")
    b = _spawn_and_finish(s, "w2", "B", status="verified", summary="B1")
    s.record_sibling_duel(a.state_id, b.state_id, DuelOutcome.COMPARABLE_TIE)
    s.record_sibling_duel(a.state_id, b.state_id, DuelOutcome.COMPARABLE_TIE)
    assert s.ledger.exhausted(a.state_id, b.state_id) is True
    # 止损后不再是候选对
    assert s.sibling_duel_candidates() == []


def test_quota_respects_budget_and_progress():
    s = SearchSession()
    _spawn_and_finish(s, "w1", "A", status="verified", summary="A1")
    _spawn_and_finish(s, "w2", "B", status="verified", summary="B1")
    q = s.quota(4)
    assert q.total <= 4
    assert q.exploit >= 1


def test_falsify_prunes_dependent_nodes_without_deleting():
    s = SearchSession()
    node = _spawn_and_finish(s, "w1", "assume ansatz Q works", status="failed", summary="need Q")
    pruned = s.falsify("ansatz Q")
    assert node in pruned
    assert s.graph.get(node.state_id).status == NodeStatus.PRUNED
    assert s.graph.get(node.state_id).prune_reason
    assert node.state_id in s.graph  # 不删除


def test_advise_packs_signals_and_advances_cycle():
    s = SearchSession()
    _spawn_and_finish(s, "w1", "A", status="verified", summary="A1")
    _spawn_and_finish(s, "w2", "B", status="failed", summary="gap")
    assert s.cycle == 0
    advice = s.advise(available_worker_slots=3)
    assert advice["cycle"] == 0
    assert "frontier_by_proxy" in advice
    assert len(advice["frontier_by_proxy"]) == 2
    assert advice["quota"]["budget"] == 3
    assert advice["solved"] is False
    # advise 推进一个 selection cycle
    assert s.cycle == 1


def test_solved_flag_propagates():
    s = SearchSession()
    _spawn_and_finish(
        s, "w1", "finish it", status="verified",
        summary="resolves problem", verified_file="verified_propositions/final.md", solved=True,
    )
    advice = s.advise(available_worker_slots=0)
    assert advice["solved"] is True
