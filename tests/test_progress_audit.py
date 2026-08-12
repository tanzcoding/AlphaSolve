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
