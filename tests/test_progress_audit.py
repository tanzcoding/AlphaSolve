import json
import time
from types import SimpleNamespace

from alphasolve.agent import AgentConfig
from alphasolve.solver import orchestrator as orchestrator_module
from alphasolve.solver import progress_audit as progress_audit_module
from alphasolve.solver.orchestrator import Orchestrator, WorkerManager
from alphasolve.solver.progress_audit import ProgressAuditQueue
from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.research_state import ResearchStateStore
from alphasolve.solver.worker import WorkerRunResult


def _outcome(
    worker_id: str,
    *,
    status: str,
    summary: str,
    failure_kind: str = "",
    rubric: str = "",
    method_id: str = "direct_proof",
) -> dict:
    return {
        "worker_id": worker_id,
        "worker_dir": f"/tmp/{worker_id}",
        "status": status,
        "summary": summary,
        "failure_kind": failure_kind,
        "direction_id": "main-direction",
        "gap_id": "terminal-gap",
        "method_id": method_id,
        "rubric": rubric,
        "solved_problem": False,
    }


def test_progress_audit_preserves_structured_difficulty_handoff(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    queue = ProgressAuditQueue(layout=layout, suite=object(), client_factory=lambda _config: None)
    payload = _outcome("difficulty-worker", status="rejected", summary="A global step remains.")
    payload["difficulty_handoff"] = {
        "worker_id": "difficulty-worker",
        "direction_id": "main-direction",
        "gap_id": "terminal-gap",
        "method_id": "matching",
        "disposition": "active_candidate",
        "blocking_obligation": "Establish the missing global compatibility bound.",
        "last_verified_step": "The local matching lemma is proved.",
        "why_current_route_fails": "No argument connects local matches globally.",
        "suggested_attack": "Study the compatibility graph.",
        "evidence_refs": ["unverified_propositions/prop-x/review.md"],
    }

    record = queue._outcome_record(payload, sequence=1)
    rendered = "\n".join(
        progress_audit_module._render_outcomes([record], layout.workspace_dir, impacts={})
    )

    assert record["difficulty_handoff"]["blocking_obligation"].startswith("Establish the missing")
    assert "Structured Difficulty Handoff" in rendered
    assert "The local matching lemma is proved." in rendered


def test_progress_audit_persists_verified_and_failed_outcomes(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()

    def review_runner(_evidence_path, _prompt):
        return """### Progress Verdict
VERDICT: MISALIGNED
### Current Best Verified Position
No verified statement closes the target.
### Terminal Gap
Prove the global target.
### Outcome Classification
The verified local lemma is incidental; the rejected attempt failed.
### Repeated Avoided Obligation
No direct attack on the global target.
### Recommended Next Action
Prove the global target exactly.
### Cited Evidence
Outcome ledger entries 1 and 2.
BLOCKER_DIRECTION_ID: main-direction
BLOCKER_GAP_ID: global-target-blocker
BLOCKER_STATEMENT: Prove the global target without assuming the missing connection.
BLOCKER_OCCURRENCE_COUNT: 2
BLOCKER_APPROACHES_JSON: [{"direction_id":"main-direction","gap_id":"terminal-gap","method_id":"direct_proof","occurrence_count":1,"description":"Local lemma route stopped before the global connection."},{"direction_id":"main-direction","gap_id":"terminal-gap","method_id":"falsification","occurrence_count":1,"description":"Counterexample-oriented route also left the global connection open."}]
"""

    audit_queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=2,
        audit_runner=review_runner,
    )
    audit_queue.start()
    try:
        assert audit_queue.record_outcome(_outcome("worker-1", status="verified", summary="local lemma"))
        assert audit_queue.record_outcome(
            _outcome(
                "worker-2",
                status="rejected",
                summary="proof gap",
                failure_kind="verification_rejected",
                method_id="falsification",
            )
        )
        assert not audit_queue.record_outcome(_outcome("worker-2", status="rejected", summary="duplicate"))

        deadline = time.time() + 2
        while time.time() < deadline:
            latest = audit_queue.status_payload()["latest"]
            if latest and latest.get("status") == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("progress audit did not finish")

        checkpoint = layout.progress_audits_dir / "checkpoint-0002"
        evidence = (checkpoint / "evidence.md").read_text(encoding="utf-8")
        assert "### Outcome 1: verified" in evidence
        assert "### Outcome 2: rejected" in evidence
        assert "verification_rejected" in evidence
        assert "Progress Audit Evidence: checkpoint-0002" in evidence

        decision = json.loads((checkpoint / "decision.json").read_text(encoding="utf-8"))
        assert decision["status"] == "completed"
        assert decision["verdict"] == "MISALIGNED"
        assert decision["terminal_gap"] == "Prove the global target."
        assert decision["recommended_next_action"] == "Prove the global target exactly."
        assert decision["repeated_avoided_obligation"] == {
            "direction_id": "main-direction",
            "gap_id": "global-target-blocker",
            "statement": "Prove the global target without assuming the missing connection.",
        }

        state = audit_queue.status_payload()
        assert state["outcomes_recorded"] == 2
        assert state["latest"]["checkpoint_id"] == "checkpoint-0002"
    finally:
        audit_queue.stop()


def test_portfolio_checkpoint_brief_joins_rubric_impact_and_curator_task(tmp_path):
    class CapturingCuratorQueue:
        def __init__(self):
            self.tasks = []

        def submit(self, task):
            self.tasks.append(task)

    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    state = ResearchStateStore(layout.verified_dir)
    state.ensure_initialized()
    state.record_impact(
        worker_id="portfolio-1",
        direction_id="main-direction",
        gap_id="terminal-gap",
        impact={
            "relation_to_target": "necessary_condition_only",
            "gap_effect": "unchanged",
            "continuation_value": "low",
            "summary": "Local fact does not connect to the terminal gap.",
            "rubric_assessment": {
                "checks": [
                    {"criterion": "Connect globally", "passed": False, "evidence": "No global inequality."},
                ]
            },
        },
    )

    def audit_runner(_evidence_path, _prompt):
        return """### Progress Verdict
VERDICT: MISALIGNED
### Current Best Verified Position
No global result.
### Terminal Gap
Prove a global connection.
### Outcome Classification
The local result is incidental.
### Repeated Avoided Obligation
Global connection missing.
### Recommended Next Action
Prove the global connection.
### Cited Evidence
Outcome evidence.
BLOCKER_DIRECTION_ID: main-direction
BLOCKER_GAP_ID: global-connection-blocker
BLOCKER_STATEMENT: Prove the global connection without assuming it.
"""

    curator_queue = CapturingCuratorQueue()
    audit_queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        curator_queue=curator_queue,
        outcomes_per_audit=2,
        audit_runner=audit_runner,
    )
    audit_queue.start()
    try:
        audit_queue.record_outcome(
            _outcome(
                "portfolio-1",
                status="verified",
                summary="local fact",
                rubric="- Connect globally\n- Use verified prerequisites",
                method_id="counting",
            )
        )
        audit_queue.record_outcome(
            _outcome("portfolio-2", status="rejected", summary="proof gap", method_id="induction")
        )
        deadline = time.time() + 2
        while time.time() < deadline and not curator_queue.tasks:
            time.sleep(0.01)
        assert curator_queue.tasks

        checkpoint = layout.progress_audits_dir / "checkpoint-0002"
        evidence = (checkpoint / "evidence.md").read_text(encoding="utf-8")
        brief = (checkpoint / "curator_brief.md").read_text(encoding="utf-8")
        assert "Pre-dispatch Rubric" in evidence
        assert "Rubric score: `0/1`" in evidence
        assert "Portfolio Curation Brief" in brief
        assert "The curator must independently decide persistence" in brief
        assert "Required Comparison Questions" in brief
        task = curator_queue.tasks[0]
        assert task.task_kind == "portfolio_checkpoint"
        assert task.artifact_path == checkpoint / "curator_brief.md"
    finally:
        audit_queue.stop()


def test_stalled_audits_accumulate_no_progress_and_request_context_reset(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n\nProve the target.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()

    def audit_runner(_evidence_path, _prompt):
        return """### Progress Verdict
VERDICT: STALLED
### Current Best Verified Position
No verified result closes the target.
### Terminal Gap
Connect the local facts to the target.
### Outcome Classification
Both outcomes are incidental.
### Repeated Avoided Obligation
The global connection remains unproved.
### Recommended Next Action
Prove the global connection proposition.
### Cited Evidence
Outcome ledger entries.
"""

    audit_queue = ProgressAuditQueue(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        outcomes_per_audit=2,
        no_progress_outcomes_before_reset=2,
        audit_runner=audit_runner,
    )
    audit_queue.start()
    try:
        audit_queue.record_outcome(_outcome("stalled-1", status="verified", summary="local fact"))
        audit_queue.record_outcome(_outcome("stalled-2", status="rejected", summary="proof gap"))
        deadline = time.time() + 2
        while time.time() < deadline:
            latest = audit_queue.status_payload()["latest"]
            if latest and latest.get("status") == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("stalled audit did not finish")

        latest = audit_queue.status_payload()["latest"]
        assert latest["no_progress_outcomes"] == 2
        assert latest["no_progress_threshold"] == 2
        assert latest["context_reset_required"] is True
    finally:
        audit_queue.stop()


def test_worker_manager_publishes_outcome_before_task_output(tmp_path, monkeypatch):
    class ImmediateWorker:
        def __init__(self, *, layout, **_kwargs):
            self.worker_id = "audit-worker"
            self.worker_dir = layout.unverified_dir / "prop-audit-worker"

        def run(self):
            return WorkerRunResult(
                worker_id=self.worker_id,
                worker_dir=self.worker_dir,
                status="rejected",
                summary="A proof gap remained.",
                failure_kind="verification_rejected",
                direction_id="main-direction",
                gap_id="terminal-gap",
                method_id="direct_proof",
            )

    class RecordingAuditQueue:
        def __init__(self):
            self.outcomes = []

        def record_outcome(self, payload):
            self.outcomes.append(payload)
            return True

        def status_payload(self):
            return {"latest": None}

    monkeypatch.setattr(orchestrator_module, "Worker", ImmediateWorker)
    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    recorder = RecordingAuditQueue()
    manager = WorkerManager(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        max_workers=1,
        max_verify_rounds=1,
        verifier_scaling_factor=1,
        subagent_max_depth=0,
        progress_audit_queue=recorder,
    )
    try:
        manager.spawn("audit this worker")
        deadline = time.time() + 1
        while time.time() < deadline and not recorder.outcomes:
            time.sleep(0.01)
        assert len(recorder.outcomes) == 1
        assert recorder.outcomes[0]["worker_id"] == "audit-worker"
        assert recorder.outcomes[0]["status"] == "rejected"
        assert recorder.outcomes[0]["failure_kind"] == "verification_rejected"
    finally:
        manager.close(timeout=0)


def test_progress_audit_uses_independent_process_auditor(tmp_path, monkeypatch):
    class CapturingAgent:
        created = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.created.append(self)

        def run(self, task, *, description=""):
            self.task = task
            self.description = description
            return SimpleNamespace(final_answer="independent audit output")

    monkeypatch.setattr(progress_audit_module, "Agent", CapturingAgent)
    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    config = AgentConfig(
        name="process_auditor",
        system_prompt="Audit only.",
        tools=(),
        max_turns=1,
    )
    audit_queue = ProgressAuditQueue(
        layout=layout,
        suite=SimpleNamespace(agents={"process_auditor": config}, subagents={}),
        client_factory=lambda _config: object(),
    )

    assert audit_queue._run_auditor("audit the checkpoint") == "independent audit output"
    assert len(CapturingAgent.created) == 1
    created = CapturingAgent.created[0]
    assert created.kwargs["config"] is config
    assert created.description == "Periodic strategic progress audit"
    assert created.task == "audit the checkpoint"


def test_process_audit_reset_injects_one_mandatory_reviewer_report(tmp_path):
    class StubAuditQueue:
        def __init__(self):
            self.handled = []

        def mark_context_reset_handled(self, checkpoint_id):
            self.handled.append(checkpoint_id)

    class StubReviewerService:
        def __init__(self):
            self.calls = []

        def call(self, agent_type, description, prompt):
            self.calls.append((agent_type, description, prompt))
            return "[research_reviewer]\n### Research Plan\nAttack the fresh terminal gap."

    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    audit_path = layout.progress_audits_dir / "checkpoint-0008" / "audit.md"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text("# Audit\n\nVERDICT: STALLED\n", encoding="utf-8")
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.layout = layout
    orchestrator.log_session = None
    orchestrator._pending_context_reset = None
    orchestrator._reset_reviewer_service = StubReviewerService()
    orchestrator._handled_process_audit_resets = set()
    audit_queue = StubAuditQueue()
    manager = SimpleNamespace(progress_audit_queue=audit_queue)
    payload = {
        "progress_audit": {
            "latest": {
                "checkpoint_id": "checkpoint-0008",
                "context_reset_required": True,
                "context_reset_handled": False,
                "verdict": "STALLED",
                "terminal_gap": "global connection",
                "audit_path": "progress_audits/checkpoint-0008/audit.md",
            }
        }
    }

    reset = orchestrator._handle_process_audit_reset(payload, manager)

    assert reset is not None
    assert "mandatory research_reviewer" in reset["message"]
    assert audit_queue.handled == []
    assert (audit_path.parent / "strategy_transition.md").is_file()
    directive = orchestrator._consume_process_audit_context_reset()
    assert directive is not None
    assert "Begin a fresh orchestrator session" in directive
    assert "Mandatory Fresh Research Reviewer Assessment" in directive
    assert "Attack the fresh terminal gap" in directive
    assert audit_queue.handled == ["checkpoint-0008"]
    service = orchestrator._reset_reviewer_service
    assert len(service.calls) == 1
    assert service.calls[0][0] == "research_reviewer"
    assert "checkpoint-0008" in service.calls[0][2]
    assert orchestrator._consume_process_audit_context_reset() is None
    assert len(service.calls) == 1
