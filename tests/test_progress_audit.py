from __future__ import annotations

import json

from alphasolve.solver.progress_audit import (
    ProgressAuditQueue,
    ProgressAuditTask,
    _render_evidence,
    _write_curation_input,
    plan_outcome_assessments,
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


def test_task_output_batch_respects_the_outcome_sampling_interval(tmp_path):
    """Process audit is long-horizon: a TaskOutput batch must not force a checkpoint.

    Regression: gating every batch fired the composite "is there real progress, is the
    plan complete, is the plan itself wrong" audit once per dispatch round, which both
    destroyed the `outcomes_per_audit` window and reduced a long-horizon reviewer to a
    per-batch rubber stamp. Short-horizon delivery is the task auditor's job.
    """
    layout = _layout(tmp_path)
    audited_checkpoints: list[str] = []

    def audit_runner(evidence_path, _prompt):
        audited_checkpoints.append(evidence_path.parent.name)
        return """### Progress Verdict
VERDICT: INSUFFICIENT_EVIDENCE
### Current Best Verified Position
None.
### Terminal Gap
Establish the first bridge.
### Outcome Classification
- failed
### Repeated Avoided Obligation
None.
### Route Contract Signals
None.
### Cited Evidence
- progress_audits/checkpoint-0005/evidence.md
BLOCKER_SOURCE_DIFFICULTY_ID: NONE
BLOCKER_STATEMENT: NONE
"""

    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=5,
        audit_runner=audit_runner,
    )
    queue.start()
    try:
        # Four batches inside one interval settle 4 outcomes and must not create a gate.
        early = [
            queue.record_outcomes([
                {"worker_id": f"worker-{index}", "status": "rejected", "summary": "No bridge."},
            ])
            for index in range(4)
        ]
        assert early == [[], [], [], []]

        # The fifth settled outcome completes the interval and triggers exactly one audit.
        checkpoint_ids = queue.record_outcomes([
            {"worker_id": "worker-4", "status": "rejected", "summary": "No bridge."},
        ])
        assert checkpoint_ids == ["checkpoint-0005"]
        decisions = queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
    finally:
        queue.stop()

    assert audited_checkpoints == ["checkpoint-0005"]
    assert decisions[0]["verdict"] == "INSUFFICIENT_EVIDENCE"


def test_outcome_audit_is_derived_for_the_executed_proposal_without_rewriting_it(tmp_path):
    layout = _layout(tmp_path)
    plans_dir = layout.workspace_dir / "curation_records" / "research_plans"
    plans_dir.mkdir(parents=True)
    plan_path = plans_dir / "plan-history.json"
    plan_body = json.dumps({
        "plan_id": "plan-history",
        "recommendation": {"research_plan": {"objective": "Close the bridge.", "strategy": "Test the bridge.", "tracks": []}},
    })
    plan_path.write_text(plan_body, encoding="utf-8")

    def audit_runner(_evidence_path, _prompt):
        return """### Progress Verdict
VERDICT: STALLED
### Current Best Verified Position
None.
### Terminal Gap
Prove the bridge.
### Outcome Classification
- failed
### Repeated Avoided Obligation
The bridge.
### Route Contract Signals
- `plan-history` / `route` / `first`: inconclusive.
### Cited Evidence
- progress_audits/checkpoint-0001/evidence.md
BLOCKER_SOURCE_DIFFICULTY_ID: NONE
BLOCKER_STATEMENT: NONE
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
            {"worker_id": "worker-history", "status": "rejected", "research_plan_id": "plan-history"},
        ])
        queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
    finally:
        queue.stop()

    # The audit thread must never write research-plan files: the orchestrator is their
    # single writer, so a concurrent plan update cannot lose an audit verdict.
    assert plan_path.read_text(encoding="utf-8") == plan_body

    derived = plan_outcome_assessments(layout.workspace_dir)
    assert derived["plan-history"][0]["verdict"] == "STALLED"
    assert derived["plan-history"][0]["terminal_gap"] == "Prove the bridge."

    summary = research_plan_execution_summary(layout.workspace_dir)
    plan = next(item for item in summary if item["plan_id"] == "plan-history")
    assert plan["outcome_assessments"][0]["verdict"] == "STALLED"


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
        "route_contract_signals": "",
        "stagnation_streak": 1,
        "stagnation_level": "NONE",
        "evidence_path": "progress_audits/checkpoint-0001/evidence.md",
        "audit_path": "progress_audits/checkpoint-0001/audit.md",
    }]


def test_progress_audit_preserves_route_contract_signals_without_route_selection(tmp_path):
    layout = _layout(tmp_path)

    def audit_runner(_evidence_path, _prompt):
        return """### Progress Verdict
VERDICT: MISALIGNED
### Current Best Verified Position
A local counterexample is verified.
### Terminal Gap
Find a route compatible with the counterexample.
### Outcome Classification
- supporting
### Repeated Avoided Obligation
None.
### Route Contract Signals
- `plan-a` / `route-a` / `test-assumption`: contradicts — `verified_propositions/counterexample.md` refutes the current milestone premise; no replacement route is selected.
### Recommended Next Action
State the remaining mathematical obligation.
### Cited Evidence
- `verified_propositions/counterexample.md`
BLOCKER_SOURCE_DIFFICULTY_ID: NONE
BLOCKER_STATEMENT: NONE
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
            {"worker_id": "worker-1", "status": "verified", "summary": "Counterexample."},
        ])
        decisions = queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
    finally:
        queue.stop()

    assert decisions[0]["verdict"] == "MISALIGNED"
    assert "route-a" in decisions[0]["route_contract_signals"]
    assert "contradicts" in decisions[0]["route_contract_signals"]
    assert "replacement route" in decisions[0]["route_contract_signals"]


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


def test_stagnation_streak_accumulates_across_checkpoints_with_the_same_blocker(tmp_path):
    """Runtime bookkeeping, not LLM self-report: repeated STALLED + same blocker escalates.

    With `stagnation_force_pivot_streak=3` (configurable policy), WATCH fires at streak 1,
    ESCALATE at streak 2, and FORCE_PIVOT once the streak reaches the configured threshold.
    """
    layout = _layout(tmp_path)

    def make_audit_runner(blocker_statement: str):
        def audit_runner(_evidence_path, _prompt):
            return f"""### Progress Verdict
VERDICT: STALLED
### Current Best Verified Position
None.
### Terminal Gap
{blocker_statement}
### Outcome Classification
- failed
### Repeated Avoided Obligation
{blocker_statement}
### Route Contract Signals
None.
### Cited Evidence
- progress_audits/checkpoint/evidence.md
BLOCKER_SOURCE_DIFFICULTY_ID: shared-blocker
BLOCKER_STATEMENT: {blocker_statement}
"""
        return audit_runner

    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=1,
        stagnation_force_pivot_streak=3,
        audit_runner=make_audit_runner("Prove the m>=3 composition gate."),
    )
    queue.start()
    try:
        levels = []
        for index in range(3):
            checkpoint_ids = queue.record_outcomes([
                {"worker_id": f"worker-{index}", "status": "rejected", "summary": "No bridge."},
            ])
            decisions = queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
            levels.append((decisions[0]["stagnation_streak"], decisions[0]["stagnation_level"]))
    finally:
        queue.stop()

    assert levels == [(1, "WATCH"), (2, "ESCALATE"), (3, "FORCE_PIVOT")]


def test_stagnation_streak_resets_when_the_blocker_changes(tmp_path):
    """A genuinely different obligation must not inherit the old streak."""
    layout = _layout(tmp_path)
    statements = iter([
        "Prove the m>=3 composition gate.",
        "Prove the m>=3 composition gate.",
        "Prove a completely different obligation.",
    ])

    def audit_runner(_evidence_path, _prompt):
        statement = next(statements)
        return f"""### Progress Verdict
VERDICT: STALLED
### Current Best Verified Position
None.
### Terminal Gap
{statement}
### Outcome Classification
- failed
### Repeated Avoided Obligation
{statement}
### Route Contract Signals
None.
### Cited Evidence
- progress_audits/checkpoint/evidence.md
BLOCKER_SOURCE_DIFFICULTY_ID: shared-blocker
BLOCKER_STATEMENT: {statement}
"""

    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=1,
        stagnation_force_pivot_streak=3,
        audit_runner=audit_runner,
    )
    queue.start()
    try:
        streaks = []
        for index in range(3):
            checkpoint_ids = queue.record_outcomes([
                {"worker_id": f"worker-{index}", "status": "rejected", "summary": "No bridge."},
            ])
            decisions = queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
            streaks.append(decisions[0]["stagnation_streak"])
    finally:
        queue.stop()

    assert streaks == [1, 2, 1]


def test_stagnation_streak_resets_on_advancing_verdict(tmp_path):
    """A verdict outside STALLED/MISALIGNED (e.g. ADVANCING) must reset the streak to 0."""
    layout = _layout(tmp_path)
    reports = iter([
        """### Progress Verdict
VERDICT: STALLED
### Current Best Verified Position
None.
### Terminal Gap
Prove the gate.
### Outcome Classification
- failed
### Repeated Avoided Obligation
Prove the gate.
### Route Contract Signals
None.
### Cited Evidence
- progress_audits/checkpoint/evidence.md
BLOCKER_SOURCE_DIFFICULTY_ID: shared-blocker
BLOCKER_STATEMENT: Prove the gate.
""",
        """### Progress Verdict
VERDICT: ADVANCING
### Current Best Verified Position
A new bridge is verified.
### Terminal Gap
Prove the gate.
### Outcome Classification
- direct_advance
### Repeated Avoided Obligation
None.
### Route Contract Signals
None.
### Cited Evidence
- verified_propositions/bridge.md
BLOCKER_SOURCE_DIFFICULTY_ID: NONE
BLOCKER_STATEMENT: NONE
""",
    ])

    def audit_runner(_evidence_path, _prompt):
        return next(reports)

    queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=1,
        stagnation_force_pivot_streak=3,
        audit_runner=audit_runner,
    )
    queue.start()
    try:
        streaks = []
        for index in range(2):
            checkpoint_ids = queue.record_outcomes([
                {"worker_id": f"worker-{index}", "status": "rejected", "summary": "No bridge."},
            ])
            decisions = queue.wait_for_decisions(checkpoint_ids, timeout_seconds=2.0)
            streaks.append((decisions[0]["stagnation_streak"], decisions[0]["stagnation_level"]))
    finally:
        queue.stop()

    assert streaks == [(1, "WATCH"), (0, "NONE")]
