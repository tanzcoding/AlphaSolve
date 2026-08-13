from __future__ import annotations

import json

from alphasolve.solver.progress_audit import ProgressAuditQueue
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
