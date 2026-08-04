"""SearchSession（调度层编排门面）单元测试。

覆盖 worker 生命周期映射（on_spawn / on_worker_result）、proxy 排序、
falsification 转发、attempt DAG 崩溃恢复、以及 advise() 打包只读事实与
cycle 推进。纯算法，无 LLM、无文件系统依赖（除显式传入 tmp_path 的用例）。

调度决策（exploit 哪个方向 / 要不要自由探索）不在 SearchSession 之内——
完全交给 orchestrator 的 LLM 自主判断，故这里不再测试任何自动选择/打分逻辑。
"""
from __future__ import annotations

from alphasolve.solver.search import (
    NodeStatus,
    SearchSession,
)


def _spawn_and_finish(
    session,
    worker_id,
    hint,
    *,
    status,
    summary,
    verified_file=None,
    proposition_file=None,
    solved=False,
    assess_verified=True,
):
    node = session.on_spawn(worker_id, hint, direction_id=f"D-{worker_id}", gap_id=f"G-{worker_id}")
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
    if status == "verified" and not solved and assess_verified:
        session.record_research_impact(
            worker_id,
            {
                "relation_to_target": "direct_advance",
                "remaining_gaps": [],
                "summary": summary,
            },
        )
    return node


def test_on_spawn_creates_depth1_child_under_root():
    s = SearchSession()
    n = s.on_spawn("w1", "try route A")
    assert n.depth == 1
    assert s.root.state_id in n.parents
    assert n.status == NodeStatus.RUNNING


def test_on_spawn_is_idempotent_per_worker():
    s = SearchSession()
    a = s.on_spawn("w1", "h")
    b = s.on_spawn("w1", "h-again")
    assert a.state_id == b.state_id
    assert len(s.graph) == 2  # root + 一个节点，不重复建节点


def test_verified_result_waits_for_research_impact_before_reward():
    s = SearchSession()
    node = _spawn_and_finish(
        s,
        "w1",
        "prove lemma",
        status="verified",
        summary="lemma X holds",
        verified_file="verified_propositions/x.md",
        assess_verified=False,
    )
    assert node.status == NodeStatus.DONE
    assert node.delta.proof_ref == "verified_propositions/x.md"
    assert node.impact_status == "pending"
    assert node.insight.verified_implications == []
    assert s.pending_impact_worker_ids() == ["w1"]
    assert s.active_worker_nodes() == []

    s.record_research_impact(
        "w1",
        {
            "relation_to_target": "direct_advance",
            "remaining_gaps": [],
            "summary": "lemma X closes the assigned gap",
        },
    )
    assert node.impact_status == "assessed"
    assert node.insight.verified_implications == ["lemma X closes the assigned gap"]
    assert s.proxy_of(node.state_id).verified_implications == 1


def test_correct_but_weak_result_gets_no_positive_progress():
    s = SearchSession()
    node = _spawn_and_finish(
        s,
        "w1",
        "derive contradiction",
        status="verified",
        summary="a necessary condition holds",
        assess_verified=False,
    )
    s.record_research_impact(
        "w1",
        {
            "relation_to_target": "necessary_condition_only",
            "remaining_gaps": ["derive the requested contradiction"],
            "summary": "Correct but does not close the assigned gap",
        },
    )
    assert node.insight.verified_implications == []
    assert node.insight.open_subgoals == ["derive the requested contradiction"]
    assert s.proxy_of(node.state_id).verified_delta == 0


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


def test_falsify_prunes_dependent_nodes_without_deleting():
    s = SearchSession()
    node = _spawn_and_finish(s, "w1", "assume ansatz Q works", status="failed", summary="need Q")
    pruned = s.falsify("ansatz Q")
    assert node in pruned
    assert s.graph.get(node.state_id).status == NodeStatus.PRUNED
    assert s.graph.get(node.state_id).prune_reason
    assert node.state_id in s.graph  # 不删除


def test_advise_packs_read_only_facts_and_advances_cycle():
    s = SearchSession()
    _spawn_and_finish(s, "w1", "A", status="verified", summary="A1")
    _spawn_and_finish(s, "w2", "B", status="failed", summary="gap")
    assert s.cycle == 0
    advice = s.advise()
    assert advice["cycle"] == 0
    assert "frontier" in advice
    assert len(advice["frontier"]) == 2
    assert advice["pending_research_impacts"] == []
    assert advice["solved"] is False
    # advise 推进一个 selection cycle
    assert s.cycle == 1


def test_attempt_graph_supports_multiple_parents():
    s = SearchSession()
    a = s.on_spawn("wa", "A", direction_id="DA", gap_id="GA")
    b = s.on_spawn("wb", "B", direction_id="DB", gap_id="GB")
    child = s.on_spawn(
        "wc", "crossover", direction_id="DC", gap_id="GC",
        parent_ids=[a.state_id, b.state_id], method_id="construction",
    )
    assert set(child.parents) == {a.state_id, b.state_id}
    assert child.depth == 2


def test_attempt_graph_restores_after_restart(tmp_path):
    s = SearchSession(
        scheduler_state_path=tmp_path / "scheduler_state.json",
        attempt_graph_path=tmp_path / "attempt_graph.jsonl",
    )
    first = s.on_spawn(
        "w1", "first attempt", route_id="route-1",
        direction_id="D1", gap_id="G1", method_id="construction",
    )
    second = s.on_spawn(
        "w2", "second attempt", direction_id="D1", gap_id="G2",
        method_id="contradiction", parent_id=first.state_id,
    )
    assert second.parents == [first.state_id]

    restored = SearchSession(
        scheduler_state_path=tmp_path / "scheduler_state.json",
        attempt_graph_path=tmp_path / "attempt_graph.jsonl",
    )
    assert first.state_id in restored.graph
    assert second.state_id in restored.graph
    assert restored.graph.get(second.state_id).parents == [first.state_id]
    assert restored.graph.get(first.state_id).direction_id == "D1"
    assert restored.graph.get(first.state_id).route_id == "route-1"


def test_solved_flag_propagates():
    s = SearchSession()
    _spawn_and_finish(
        s, "w1", "finish it", status="verified",
        summary="resolves problem", verified_file="verified_propositions/final.md", solved=True,
    )
    advice = s.advise()
    assert advice["solved"] is True
