from __future__ import annotations

import json
from types import SimpleNamespace

from alphasolve.agent.tools import ToolRegistry
from alphasolve.solver.difficulty_declaration import (
    difficulty_json_path,
    load_difficulty_declaration,
    materialize_difficulty_handoff,
    register_difficulty_declaration_tool,
)
from alphasolve.solver.difficulty_portfolio import (
    build_reviewer_prompt,
    candidate_handoffs,
    merge_candidate_handoffs,
)
from alphasolve.solver.orchestrator import Orchestrator, _worker_result_payload
from alphasolve.solver.worker import WorkerRunResult


def _record_generator_difficulty(tmp_path):
    worker_dir = tmp_path / "prop-worker-a"
    worker_dir.mkdir()
    (worker_dir / "research_target.json").write_text(
        json.dumps({
            "worker_id": "worker-a",
            "direction_id": "lower-bound",
            "gap_id": "global-packing",
            "method_id": "matching",
            "hint": "Prove the global packing lower bound.",
        }),
        encoding="utf-8",
    )
    declaration_path = worker_dir / "difficulty_declaration.md"
    registry = ToolRegistry()
    register_difficulty_declaration_tool(
        registry,
        declaration_path=declaration_path,
        role="generator",
    )
    result = registry.execute(
        "RecordDifficulty",
        {
            "target_status": "PARTIAL",
            "difficulty": "Prove a global packing bound for the extra marker family.",
            "last_verified_step": "The two-family matching gives the N+1 lower bound.",
            "why_hard": "The argument controls local matches but not the global packing number.",
            "suggested_attack": "Analyze the marker conflict graph as an independent-set problem.",
            "dead_ends": "Repeating the two-family count cannot create the missing third-family bound.",
        },
    )
    assert not result.is_error
    return worker_dir, declaration_path


def test_record_difficulty_writes_structured_sidecar_with_runtime_context(tmp_path):
    worker_dir, declaration_path = _record_generator_difficulty(tmp_path)

    assert declaration_path.is_file()
    sidecar = difficulty_json_path(declaration_path)
    assert sidecar.is_file()
    declaration = load_difficulty_declaration(declaration_path)

    assert declaration is not None
    assert declaration["runtime_context"]["worker_id"] == "worker-a"
    assert declaration["runtime_context"]["direction_id"] == "lower-bound"
    assert declaration["generator"]["last_verified_step"].startswith("The two-family")
    assert "Structured" not in declaration_path.read_text(encoding="utf-8")
    assert worker_dir.is_dir()


def test_materialized_handoff_is_candidate_with_final_evidence_paths(tmp_path):
    worker_dir, declaration_path = _record_generator_difficulty(tmp_path)
    review = worker_dir / "review.md"
    review.write_text("Verdict: fail\nThe global packing step is absent.\n", encoding="utf-8")
    summary = worker_dir / "result_summary.md"
    summary.write_text("### Core Difficulty\nUnchanged global packing obligation.\n", encoding="utf-8")
    proposition = worker_dir / "proposition.md"
    proposition.write_text("## Statement\nClaim\n## Proof\nIncomplete.\n", encoding="utf-8")

    handoff_path, handoff = materialize_difficulty_handoff(
        declaration_path=declaration_path,
        worker_id="worker-a",
        direction_id="lower-bound",
        gap_id="global-packing",
        method_id="matching",
        execution_status="rejected",
        failure_kind="verification_rejected",
        result_summary_file=summary,
        review_file=review,
        proposition_file=proposition,
        verified_file=None,
    )

    assert handoff_path is not None and handoff_path.is_file()
    assert handoff is not None
    assert handoff["disposition"] == "active_candidate"
    assert handoff["blocking_obligation"].startswith("Prove a global packing")
    assert str(review) in handoff["evidence_refs"]
    assert handoff["requires_portfolio_comparison"] is True


def test_worker_payload_and_portfolio_preserve_candidate_handoffs(tmp_path):
    worker_dir, declaration_path = _record_generator_difficulty(tmp_path)
    handoff_path, handoff = materialize_difficulty_handoff(
        declaration_path=declaration_path,
        worker_id="worker-a",
        direction_id="lower-bound",
        gap_id="global-packing",
        method_id="matching",
        execution_status="rejected",
        failure_kind="verification_rejected",
        result_summary_file=None,
        review_file=None,
        proposition_file=None,
        verified_file=None,
    )
    assert handoff_path is not None and handoff is not None
    result = WorkerRunResult(
        worker_id="worker-a",
        worker_dir=worker_dir,
        status="rejected",
        summary="The packing proof is incomplete.",
        direction_id="lower-bound",
        gap_id="global-packing",
        method_id="matching",
        difficulty_declaration_file=declaration_path,
        difficulty_handoff_file=handoff_path,
        difficulty_handoff=handoff,
        failure_kind="verification_rejected",
    )

    payload = _worker_result_payload(result)
    candidates = candidate_handoffs([payload])
    merged = merge_candidate_handoffs(candidates, candidates)

    assert payload["difficulty_handoff_file"] == str(handoff_path)
    assert candidates[0]["worker_id"] == "worker-a"
    assert len(merged) == 1
    assert "Difficulty Portfolio" in build_reviewer_prompt(merged)


def test_orchestrator_requests_reviewer_for_multiple_difficulty_candidates(tmp_path):
    class ReviewerService:
        def __init__(self):
            self.calls = []

        def call(self, agent_type, description, prompt):
            self.calls.append((agent_type, description, prompt))
            return "### Difficulty Comparison\nSAME: attack the shared packing obligation."

    first = {
        "worker_id": "worker-a",
        "direction_id": "route-a",
        "gap_id": "gap-a",
        "method_id": "matching",
        "disposition": "active_candidate",
        "blocking_obligation": "Establish the missing global packing bound.",
        "last_verified_step": "Two-family matching is proved.",
        "why_current_route_fails": "No global packing inequality.",
        "suggested_attack": "Study the conflict graph.",
        "evidence_refs": ["a/review.md"],
    }
    second = {
        **first,
        "worker_id": "worker-b",
        "direction_id": "route-b",
        "method_id": "fooling_set",
        "evidence_refs": ["b/review.md"],
    }
    service = ReviewerService()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = SimpleNamespace(progress_audit_outcomes_path=tmp_path / "missing.jsonl")
    orchestrator.log_session = None
    orchestrator._reset_reviewer_service = service
    orchestrator._reviewed_difficulty_batches = set()
    orchestrator._difficulty_review_reports = {}

    portfolio = orchestrator._difficulty_portfolio_payload({
        "completed": [
            {"worker_id": "worker-a", "difficulty_handoff": first},
            {"worker_id": "worker-b", "difficulty_handoff": second},
        ]
    })

    assert portfolio is not None
    assert portfolio["review_status"] == "reviewed"
    assert "SAME" in portfolio["research_reviewer_report"]
    assert service.calls[0][0] == "research_reviewer"
    assert "Difficulty Portfolio" in service.calls[0][2]
