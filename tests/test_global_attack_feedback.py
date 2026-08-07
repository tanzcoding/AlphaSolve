from alphasolve.solver.difficulty_dag import (
    GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL,
    DifficultyDagStore,
)
from alphasolve.solver.orchestrator import Orchestrator
from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.worker import WorkerRunResult


def _layout(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return layout


def _add_verified(layout, start, count):
    for index in range(start, start + count):
        (layout.verified_dir / f"lemma-{index}.md").write_text(
            f"## Statement\nLemma {index}.\n\n## Proof\nProof.\n",
            encoding="utf-8",
        )


def test_global_attack_requires_verified_proposition_interval(tmp_path):
    layout = _layout(tmp_path)
    dag = DifficultyDagStore(layout.workspace_dir)

    initial = dag.global_attack_preflight()
    assert initial["ready"] is False
    assert initial["reason"] == "initial_verified_proposition_threshold_not_met"
    assert initial["remaining_verified_propositions"] == GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL

    _add_verified(layout, 1, GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL)
    ready = dag.global_attack_preflight()
    assert ready["ready"] is True
    assert ready["reason"] == "initial_verified_proposition_threshold_met"

    dag.begin_global_attack(worker_id="worker-global-1")
    assert dag.global_attack_preflight()["reason"] == "global_attack_in_flight"
    dag.complete_global_attack(
        worker_id="worker-global-1",
        status="rejected",
        target_achieved=False,
        solved_problem=False,
        result_path="curation_records/global-attacks/global-worker-global-1.md",
    )

    blocked = dag.global_attack_preflight()
    assert blocked["ready"] is False
    assert blocked["reason"] == "verified_proposition_interval_not_met"
    assert blocked["verified_propositions_since_last_global_attack"] == 0
    assert blocked["remaining_verified_propositions"] == GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL

    _add_verified(layout, 100, GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL - 1)
    assert dag.global_attack_preflight()["ready"] is False

    _add_verified(layout, 200, 1)
    ready_again = dag.global_attack_preflight()
    assert ready_again["ready"] is True
    assert ready_again["reason"] == "verified_proposition_interval_met"
    assert ready_again["verified_propositions_since_last_global_attack"] == GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL


class _CapturingCuratorQueue:
    def __init__(self):
        self.tasks = []

    def submit(self, task):
        self.tasks.append(task)


def test_global_attack_completion_writes_report_and_submits_curator_review(tmp_path):
    layout = _layout(tmp_path)
    _add_verified(layout, 1, GLOBAL_ATTACK_VERIFIED_PROPOSITION_INTERVAL)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.begin_global_attack(worker_id="worker-global-1")
    curator_queue = _CapturingCuratorQueue()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = layout
    orchestrator.difficulty_dag = dag
    orchestrator.curator_queue = curator_queue

    result = WorkerRunResult(
        worker_id="worker-global-1",
        worker_dir=layout.unverified_dir / "prop-worker-global-1",
        status="rejected",
        summary="The proposed synthesis cannot bridge the final implication.",
        difficulty_id="global-problem-attack",
        is_consolidation=True,
        target_achieved=False,
        pinned_target="Prove the target.",
        failure_kind="verification_rejected",
        blocking_obligation="Establish the bridge implication.",
        verify_history=[{"round": 1, "verdict": "fail", "review_excerpt": "The bridge is unsupported."}],
    )

    feedback = orchestrator._handle_worker_completion(result)

    report = layout.workspace_dir / feedback["global_attack_report"]
    assert report.is_file()
    report_text = report.read_text(encoding="utf-8")
    assert "Establish the bridge implication." in report_text
    assert "Prove the target." in report_text
    assert "verified_propositions/lemma-1.md" in report_text
    assert feedback["global_consolidation_directive"]["reason"] == "verified_proposition_interval_not_met"
    assert len(curator_queue.tasks) == 1
    assert curator_queue.tasks[0].task_kind == "global_attack_review"
    assert curator_queue.tasks[0].artifact_path == report
