from __future__ import annotations

import json

from alphasolve.solver.progress_audit import (
    ProgressAuditQueue,
    ProgressAuditTask,
    _write_curation_input,
)
from alphasolve.solver.project import ProjectLayout


def _layout(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return layout


def test_periodic_audit_records_immutable_worker_outcome(tmp_path):
    layout = _layout(tmp_path)
    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=1,
    )
    assert queue.record_outcome({
        "worker_id": "worker-1",
        "difficulty_id": "leaf",
        "method_id": "construction",
        "status": "verified",
        "summary": "Verified a local bridge.",
        "verified_file": str(layout.verified_dir / "bridge.md"),
    })
    record = json.loads(layout.progress_audit_outcomes_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["difficulty_id"] == "leaf"
    assert record["status"] == "verified"
    assert queue.status_payload()["pending_checkpoints"] == ["checkpoint-0001"]


def test_targeted_verified_outcome_forces_timely_curation_checkpoint(tmp_path):
    layout = _layout(tmp_path)
    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=5,
    )

    assert queue.record_outcome({
        "worker_id": "worker-targeted",
        "difficulty_id": "claimed-lower-bound",
        "status": "verified",
        "summary": "Verified a construction contradicting the claimed lower bound.",
        "verified_file": str(layout.verified_dir / "counterexample.md"),
    })

    assert queue.status_payload()["pending_checkpoints"] == []
    checkpoint_dir = layout.workspace_dir / "curation_records" / "evidence_checkpoints" / "targeted-verified-0001"
    ready = json.loads((checkpoint_dir / "curation_ready.json").read_text(encoding="utf-8"))
    assert ready["trigger_reason"] == "targeted_verified_evidence"
    assert (checkpoint_dir / "curation_input.json").is_file()
    assert (checkpoint_dir / "curator_brief.md").is_file()


def test_startup_recovers_missed_targeted_verified_evidence(tmp_path):
    layout = _layout(tmp_path)
    layout.progress_audit_outcomes_path.write_text(json.dumps({
        "sequence": 1,
        "worker_id": "worker-1",
        "difficulty_id": "claimed-bound",
        "status": "verified",
    }) + "\n", encoding="utf-8")
    layout.progress_audit_state_path.write_text(json.dumps({
        "outcome_count": 1,
        "last_scheduled_outcome": 0,
        "recorded_worker_ids": ["worker-1"],
        "pending_checkpoints": [],
    }), encoding="utf-8")
    queue = ProgressAuditQueue(layout=layout, suite=object(), client_factory=lambda _config: None)

    with queue._lock:
        queue._schedule_recovered_targeted_verified_checkpoint_locked()

    assert queue.status_payload()["pending_checkpoints"] == []
    checkpoint_dir = layout.workspace_dir / "curation_records" / "evidence_checkpoints" / "targeted-verified-recovery-0001-0001"
    ready = json.loads((checkpoint_dir / "curation_ready.json").read_text(encoding="utf-8"))
    assert ready["trigger_reason"] == "recovered_targeted_verified_evidence"


def test_curation_input_exposes_handoff_and_targeted_verified_evidence(tmp_path):
    layout = _layout(tmp_path)
    layout.progress_audit_outcomes_path.write_text(json.dumps({
        "sequence": 1,
        "worker_id": "worker-1",
        "difficulty_id": "claimed-bound",
        "method_id": "construction",
        "status": "verified",
        "summary": "A construction is below the claimed bound.",
        "verified_file": "verified_propositions/counterexample.md",
        "difficulty_handoff": {
            "handoff_id": "handoff-worker-1",
            "assigned_target": "Prove the claimed bound.",
            "obstacle": "The construction contradicts the claimed bound.",
            "obstacle_records": [{
                "role": "reasoning_subagent",
                "obstacle": "The equality case fails for the candidate lower bound.",
                "delegated_description": "Check candidate lower bound",
                "delegated_task": "Prove the candidate lower bound under the stated construction.",
                "subagent_session_id": "worker-1/reasoning/1",
            }],
            "evidence_refs": ["verified_propositions/counterexample.md"],
        },
    }) + "\n", encoding="utf-8")
    checkpoint_dir = layout.progress_audits_dir / "checkpoint-0001"
    checkpoint_dir.mkdir(parents=True)
    task = ProgressAuditTask(
        checkpoint_id="checkpoint-0001",
        watermark=1,
        previous_watermark=0,
        evidence_path=checkpoint_dir / "evidence.md",
        checkpoint_dir=checkpoint_dir,
    )

    path = _write_curation_input(layout=layout, task=task)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["canonical_dag_path"] == "curation_records/difficulty_dag.json"
    assert payload["worker_obstacles"][0]["handoff_id"] == "handoff-worker-1"
    reasoning = payload["worker_obstacles"][0]["obstacle_records"][0]
    assert reasoning["role"] == "reasoning_subagent"
    assert reasoning["delegated_description"] == "Check candidate lower bound"
    assert reasoning["delegated_task"].startswith("Prove the candidate lower bound")
    assert payload["targeted_verified_results"][0]["assigned_difficulty_id"] == "claimed-bound"


def test_duplicate_worker_outcome_is_ignored(tmp_path):
    layout = _layout(tmp_path)
    queue = ProgressAuditQueue(layout=layout, suite=object(), client_factory=lambda _config: None)
    payload = {"worker_id": "worker-1", "status": "rejected", "summary": "No progress."}
    assert queue.record_outcome(payload)
    assert not queue.record_outcome(payload)


def test_task_output_batch_waits_for_compact_checkpoint_decision(tmp_path):
    layout = _layout(tmp_path)

    def audit_runner(_evidence_path, _prompt):
        return """### Progress Verdict
VERDICT: STALLED
### Current Best Verified Position
None.
### Terminal Gap
Prove the missing bridge lemma.
### Outcome Classification
- failed
### Repeated Avoided Obligation
The bridge lemma.
### Recommended Next Action
Prove the bridge lemma under the current boundary.
### Cited Evidence
- progress_audits/checkpoint-0001/evidence.md
BLOCKER_SOURCE_DIFFICULTY_ID: worker-1-difficulty
BLOCKER_STATEMENT: Prove the missing bridge lemma.
"""

    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=1,
        audit_runner=audit_runner,
    )
    queue.start()
    try:
        checkpoint_ids = queue.record_outcomes([
            {"worker_id": "worker-1", "status": "rejected", "summary": "No verified bridge."},
        ])
        assert checkpoint_ids == ["checkpoint-0001"]
        decisions = queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
    finally:
        queue.stop()

    assert decisions == [{
        "checkpoint_id": "checkpoint-0001",
        "watermark": 1,
        "status": "completed",
        "verdict": "STALLED",
        "terminal_gap": "Prove the missing bridge lemma.",
        "repeated_avoided_obligation": {
            "source_difficulty_id": "worker-1-difficulty",
            "statement": "Prove the missing bridge lemma.",
        },
        "recommended_next_action": "Prove the bridge lemma under the current boundary.",
        "evidence_path": "progress_audits/checkpoint-0001/evidence.md",
        "audit_path": "progress_audits/checkpoint-0001/audit.md",
    }]
