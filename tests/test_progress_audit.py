from __future__ import annotations

import json

from alphasolve.solver.progress_audit import (
    ProgressAuditQueue,
    ProgressAuditTask,
    _render_evidence,
    _write_curation_input,
    research_plan_execution_summary,
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
        "research_plan_id": "plan-bridge",
        "research_track_id": "bridge-track",
        "track_priority": "primary",
    })
    record = json.loads(layout.progress_audit_outcomes_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["difficulty_id"] == "leaf"
    assert record["status"] == "verified"
    assert record["research_plan_id"] == "plan-bridge"
    assert record["research_track_id"] == "bridge-track"
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
        "recommended_next_action": "",
        "evidence_path": "progress_audits/checkpoint-0001/evidence.md",
        "audit_path": "progress_audits/checkpoint-0001/audit.md",
    }]


def test_process_audit_evidence_joins_global_plan_tracks_to_proposition_refs(tmp_path):
    layout = _layout(tmp_path)
    plans_dir = layout.workspace_dir / "curation_records" / "research_plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "plan-route.json").write_text(json.dumps({
        "plan_id": "plan-route",
        "execution_status": "executed",
        "selected_track_ids": ["primary"],
        "spawned_worker_ids": ["worker-1"],
        "recommendation": {
            "research_plan": {
                "objective": "Close the bridge.",
                "strategy": "Use exact variance and avoid the refuted gap relaxation.",
                "tracks": [{
                    "track_id": "primary",
                    "priority": "primary",
                    "kind": "TARGET_NODE",
                    "route_label": "exact-variance",
                    "research_goal": "Prove the remaining bridge.",
                    "avoid": "Do not use the gap relaxation.",
                }],
            },
        },
    }), encoding="utf-8")
    verified = layout.verified_dir / "bridge.md"
    verified.write_text("## Statement\nA bridge.\n", encoding="utf-8")
    outcomes = [{
        "sequence": 1,
        "worker_id": "worker-1",
        "status": "verified",
        "delivery": "off_target",
        "research_plan_id": "plan-route",
        "research_track_id": "primary",
        "track_priority": "primary",
        "route_label": "exact-variance",
        "verified_file": str(verified),
        "residual_obligation": "Prove the exact local transfer.",
        "difficulty_handoff_file": "unverified_propositions/prop-worker-1/difficulty_handoff.json",
    }]

    summary = research_plan_execution_summary(layout.workspace_dir, outcomes)
    evidence = _render_evidence(
        layout=layout,
        checkpoint_id="checkpoint-0001",
        watermark=1,
        previous_watermark=0,
        outcomes=outcomes,
    )

    assert summary[0]["plan_id"] == "plan-route"
    track = summary[0]["tracks"][0]
    assert track["outcomes"][0]["verified_proposition_ref"] == "verified_propositions/bridge.md"
    assert "## Global Research Plan Execution History" in evidence
    assert "Plan `plan-route`" in evidence
    assert "`verified_propositions/bridge.md`" in evidence
    assert "Residual obligation: Prove the exact local transfer." in evidence


def test_global_plan_history_keeps_all_plans_and_track_outcomes(tmp_path):
    layout = _layout(tmp_path)
    plans_dir = layout.workspace_dir / "curation_records" / "research_plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for index in range(41):
        (plans_dir / f"plan-{index:02d}.json").write_text(json.dumps({
            "plan_id": f"plan-{index:02d}",
            "recommendation": {"research_plan": {
                "objective": "Objective",
                "strategy": "Strategy",
                "tracks": [{"track_id": "route", "route_label": "shared-route"}],
            }},
        }), encoding="utf-8")
    outcomes = [
        {
            "sequence": index + 1,
            "worker_id": f"worker-{index}",
            "research_plan_id": "plan-00",
            "research_track_id": "route",
            "status": "verified",
            "verified_file": f"verified_propositions/result-{index}.md",
        }
        for index in range(17)
    ]

    summary = research_plan_execution_summary(layout.workspace_dir, outcomes)
    first = next(plan for plan in summary if plan["plan_id"] == "plan-00")

    assert len(summary) == 41
    assert len(first["tracks"][0]["outcomes"]) == 17
