"""离线产出回流到在线生产的连线测试。

覆盖此前"只写不读 / 建好不接线"的几处断链：
- curator 维护的 common-errors.md 必须真正注入 generator 与 reviser；
- 调度侧核对过的 canonical statement 必须随派发下发给 worker；
- progress audit 完成后必须同时派 DAG curation 与 knowledge 摘要两个任务；
- attempt 谱系观测器必须收到 spawn / result 事件；
- reviewer 必须看到自己此前的策略；
- 累积证据账本必须是紧凑索引而非全量重渲染。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from alphasolve.solver.project import ProjectLayout


def _layout(tmp_path: Path) -> ProjectLayout:
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return layout


def _worker(layout: ProjectLayout, **kwargs):
    from alphasolve.solver.worker import Worker

    return Worker(
        layout=layout,
        suite=SimpleNamespace(agents={}, subagents={}, settings={}),
        client_factory=lambda _config: None,
        **kwargs,
    )


def test_common_error_patterns_reach_generator_and_reviser(tmp_path):
    layout = _layout(tmp_path)
    (layout.knowledge_dir / "common-errors.md").write_text(
        "---\nmodification_count: 4\n---\n"
        "# Common Proof Errors\n\n"
        "- Asserting a supremum is attained without proving compactness.\n"
        "- Reusing a cited lemma outside its stated hypotheses.\n",
        encoding="utf-8",
    )
    worker = _worker(layout, worker_hint="Bound the packing term.")

    generator_task = worker._generator_task()
    reviser_task = worker._reviser_task(
        layout.workspace_dir / "problem.md", "Verdict: fail", workflow_index=1
    )

    for task in (generator_task, reviser_task):
        assert "# Known Proof Error Patterns" in task
        assert "Asserting a supremum is attained without proving compactness." in task
        # frontmatter 是 curator 的记账字段，不应泄漏进任务提示。
        assert "modification_count" not in task


def test_missing_or_empty_common_errors_injects_nothing(tmp_path):
    layout = _layout(tmp_path)
    worker = _worker(layout, worker_hint="Bound the packing term.")
    assert worker._common_error_patterns_block() == ""

    (layout.knowledge_dir / "common-errors.md").write_text("# Common Proof Errors\n\n", encoding="utf-8")
    assert worker._common_error_patterns_block() == ""


def test_assigned_canonical_statement_reaches_generator(tmp_path):
    layout = _layout(tmp_path)
    worker = _worker(
        layout,
        difficulty_id="packing-leaf",
        difficulty_statement="Bound the global packing term by 2111.",
    )

    task = worker._generator_task()

    assert "# Assigned Canonical Difficulty" in task
    assert "Bound the global packing term by 2111." in task
    # 它是待证义务，不是可引用的既有事实。
    assert "not an established fact you may cite" in task

    without = _worker(layout, difficulty_id="packing-leaf")
    assert "# Assigned Canonical Difficulty" not in without._generator_task()


def test_progress_audit_submits_both_dag_curation_and_knowledge_summary(tmp_path):
    from alphasolve.solver.progress_audit import ProgressAuditQueue

    layout = _layout(tmp_path)
    submitted = []

    queue = ProgressAuditQueue(
        layout=layout,
        suite=SimpleNamespace(agents={}),
        client_factory=lambda _config: None,
        curator_queue=SimpleNamespace(submit=submitted.append),
        audit_runner=lambda _path, _prompt: (
            "### Progress Verdict\nVERDICT: STALLED\n"
            "### Terminal Gap\nThe bridge lemma is unproved.\n"
            "### Recommended Next Action\nProve the bridge lemma.\n"
        ),
        outcomes_per_audit=1,
    )
    queue.start()
    queue.record_outcome({"worker_id": "worker-a", "status": "rejected", "summary": "no bridge"})
    queue.stop()

    kinds = [task.task_kind for task in submitted]
    assert "portfolio_checkpoint" in kinds
    # 审计结论必须沉淀为可复用知识，否则失败模式永久停留在 progress_audits/。
    assert "progress_audit" in kinds
    assert kinds.index("portfolio_checkpoint") < kinds.index("progress_audit")
    audit_task = next(task for task in submitted if task.task_kind == "progress_audit")
    assert audit_task.audit_path is not None and audit_task.audit_path.is_file()


def test_cumulative_ledger_is_a_compact_index(tmp_path):
    from alphasolve.solver.progress_audit import _render_evidence

    layout = _layout(tmp_path)
    filler = "PAYLOAD" + "x" * 200
    outcomes = [
        {
            "sequence": index,
            "status": "rejected",
            "difficulty_id": "leaf",
            "method_id": "direct_proof",
            "orchestrator_session_id": "ralph-1",
            "summary": filler,
            "difficulty_declaration": filler,
            "review_excerpt": filler,
        }
        for index in range(1, 11)
    ]

    evidence = _render_evidence(
        layout=layout,
        checkpoint_id="checkpoint-0010",
        watermark=10,
        previous_watermark=9,
        outcomes=outcomes,
    )

    # delta（第 10 条）保留三段全文；历史 9 条只留索引行，不再全量重渲染。
    assert evidence.count(filler) == 3
    assert "## Cumulative Outcome Ledger (Index)" in evidence
    assert evidence.count("| 1 | rejected |") == 1
    assert "`ralph-1`" in evidence


def test_attempt_observer_receives_spawn_and_result(tmp_path):
    from alphasolve.solver.search import SearchSession

    layout = _layout(tmp_path)
    session = SearchSession(
        scheduler_state_path=layout.scheduler_state_path,
        attempt_graph_path=layout.attempt_graph_path,
    )

    session.on_spawn("worker-a", "Bound the packing term.", difficulty_id="leaf", method_id="contradiction")
    node = session.on_worker_result({
        "worker_id": "worker-a",
        "status": "verified",
        "summary": "Established the bound.",
        "difficulty_id": "leaf",
        "verified_file": "verified_propositions/bound.md",
    })

    assert node is not None
    assert node.difficulty_id == "leaf"
    assert node.method_id == "contradiction"
    assert node.delta.proof_ref == "verified_propositions/bound.md"
    # 谱系必须落盘，供进程重启后恢复。
    events = [json.loads(line) for line in layout.attempt_graph_path.read_text(encoding="utf-8").splitlines()]
    assert [item["event"] for item in events] == ["spawn", "result"]
    assert session.frontier_ranking()[0]["difficulty_id"] == "leaf"


def test_reviewer_sees_its_prior_decisions(tmp_path):
    from alphasolve.solver.subagent_service import SubagentService

    history = tmp_path / "reviewer_history.md"
    history.write_text(
        "\n---\n\n## Reviewer call: earlier\nsession: s1\ndescription: d1\n\n"
        "Long adversarial prose that should not dominate the injection.\n\n"
        "### Research Strategy JSON\n```json\n{\"next_step\": {\"kind\": \"HOLD\"}}\n```\n",
        encoding="utf-8",
    )
    service = SubagentService(
        suite=SimpleNamespace(subagents={}),
        client_factory=lambda _config: None,
        reviewer_history_path=history,
    )

    rendered = service._recent_reviewer_history()

    assert "### Research Strategy JSON" in rendered
    # 只注入决策，不重放完整对抗性复核散文。
    assert "Long adversarial prose" not in rendered

    empty = SubagentService(
        suite=SimpleNamespace(subagents={}),
        client_factory=lambda _config: None,
        reviewer_history_path=tmp_path / "missing.md",
    )
    assert empty._recent_reviewer_history() == ""


def test_malformed_graph_observation_does_not_discard_the_strategy():
    from alphasolve.solver.research_planning import parse_recommendation

    recommendation = parse_recommendation("""### Research Strategy JSON
```json
{
  "research_strategy": "Attack the bridge lemma by construction.",
  "next_step": {"kind": "NEW_DIRECTION", "difficulty_id": "", "method_id": "construction", "brief": "Construct the bridge."},
  "graph_observations": [
    {"kind": "NOT_A_KIND", "target_ids": ["leaf"], "summary": "bad kind", "evidence_refs": ["a.md"], "recommended_graph_effect": "reconsider_edge"},
    {"kind": "EDGE_SUSPECT", "target_ids": ["leaf"], "summary": "no evidence cited", "evidence_refs": [], "recommended_graph_effect": "reconsider_edge"},
    {"kind": "EDGE_SUSPECT", "target_ids": ["root", "leaf"], "summary": "The counterexample contradicts the prerequisite.", "evidence_refs": ["verified_propositions/counterexample.md"], "recommended_graph_effect": "reconsider_edge"}
  ]
}
```""")

    assert recommendation is not None
    assert recommendation["next_step"]["method_id"] == "construction"
    # 坏条目被丢弃，不再作废整次昂贵的 reviewer 调用；未引证据的观察同样丢弃。
    assert len(recommendation["graph_observations"]) == 1
    assert recommendation["graph_observations"][0]["evidence_refs"] == ["verified_propositions/counterexample.md"]
