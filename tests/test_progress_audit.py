import json
import time

from alphasolve.solver import progress_audit as audit_module
from alphasolve.solver.progress_audit import ProgressAuditQueue
from alphasolve.solver.project import ProjectLayout


def _outcome(worker_id, *, status="rejected", summary="blocked", difficulty_id="packing-leaf"):
    return {
        "worker_id": worker_id,
        "worker_dir": f"/tmp/{worker_id}",
        "status": status,
        "summary": summary,
        "failure_kind": "verification_rejected" if status == "rejected" else "",
        "difficulty_id": difficulty_id,
        "method_id": "direct_proof",
        "solved_problem": False,
    }


def test_progress_audit_preserves_recursive_difficulty_handoff(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    queue = ProgressAuditQueue(layout=layout, suite=object(), client_factory=lambda _config: None)
    payload = _outcome("worker-a")
    payload["difficulty_handoff"] = {
        "source_difficulty_id": "packing-extra-marker",
        "parent_difficulty_id": "packing-leaf",
        "relation_to_parent": "prerequisite",
        "parent_resolution_policy": "all_of",
        "disposition": "active_candidate",
        "blocking_obligation": "Control the extra marker family.",
        "last_verified_step": "Two-family matching is proved.",
        "why_current_route_fails": "The global term is uncontrolled.",
        "suggested_attack": "Analyze the conflict graph.",
        "evidence_refs": ["workers/a/difficulty_handoff.json"],
    }

    record = queue._outcome_record(payload, sequence=1)
    rendered = "\n".join(audit_module._render_outcomes([record], layout.workspace_dir, impacts={}))
    assert record["difficulty_id"] == "packing-leaf"
    assert record["difficulty_handoff"]["parent_difficulty_id"] == "packing-leaf"
    assert "Structured Difficulty Handoff" in rendered


def test_progress_audit_checkpoint_uses_difficulty_dag_view(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    (layout.workspace_dir / "curation_records").mkdir(exist_ok=True)
    (layout.workspace_dir / "curation_records" / "difficulty_dag.json").write_text(
        json.dumps({"nodes": {"packing-leaf": {"status": "open"}}}), encoding="utf-8"
    )

    def audit_runner(_path, _prompt):
        return """### Progress Verdict
VERDICT: MISALIGNED
### Current Best Verified Position
No global result.
### Terminal Gap
Control the packing term.
### Outcome Classification
The attempt is blocked.
### Repeated Avoided Obligation
Packing term remains.
### Recommended Next Action
Curate the worker handoff.
### Cited Evidence
Outcome 1.
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
        assert queue.record_outcome(_outcome("worker-a"))
        deadline = time.time() + 2
        while time.time() < deadline and not queue.status_payload()["latest"]:
            time.sleep(0.01)
        checkpoint = layout.progress_audits_dir / "checkpoint-0001"
        evidence = (checkpoint / "evidence.md").read_text(encoding="utf-8")
        decision = json.loads((checkpoint / "decision.json").read_text(encoding="utf-8"))
        assert "Current Curated Difficulty Frontier" in evidence
        assert "packing-leaf" in evidence
        assert "context_reset_required" not in decision
        assert "context_reset_handled" not in decision
        assert "no_progress_outcomes" not in queue.status_payload()
    finally:
        queue.stop()
