from __future__ import annotations

import json

from alphasolve.agent.tools import ToolRegistry
from alphasolve.solver.difficulty_declaration import (
    difficulty_json_path,
    load_difficulty_declaration,
    materialize_difficulty_handoff,
    register_difficulty_declaration_tool,
)
from alphasolve.solver.difficulty_portfolio import candidate_handoffs


def _record_generator_difficulty(tmp_path):
    worker_dir = tmp_path / "prop-worker-a"
    worker_dir.mkdir()
    (worker_dir / "research_target.json").write_text(
        json.dumps({
            "worker_id": "worker-a",
            "difficulty_id": "global-packing",
            "method_id": "matching",
            "difficulty_statement": "Prove the global packing bound.",
        }),
        encoding="utf-8",
    )
    declaration_path = worker_dir / "difficulty_declaration.md"
    registry = ToolRegistry()
    register_difficulty_declaration_tool(registry, declaration_path=declaration_path, role="generator")
    result = registry.execute(
        "RecordDifficulty",
        {
            "event_kind": "blocked",
            "source_difficulty_id": "packing-extra-marker",
            "blocking_obligation": "Control the extra marker family in the global packing bound.",
            "verified_boundary": "The two-family matching gives an N+1 lower bound.",
            "remaining_delta": "Prove the standalone marker-family estimate beyond the two-family bound.",
        },
    )
    assert not result.is_error
    return worker_dir, declaration_path


def test_record_difficulty_preserves_minimal_targeted_obstacle(tmp_path):
    worker_dir, declaration_path = _record_generator_difficulty(tmp_path)
    declaration = load_difficulty_declaration(declaration_path)

    assert declaration is not None
    assert declaration["runtime_context"]["difficulty_id"] == "global-packing"
    record = declaration["generator"]
    assert record["source_difficulty_id"] == "packing-extra-marker"
    assert record["parent_difficulty_id"] == "global-packing"
    assert record["event_kind"] == "blocked"
    assert record["verified_boundary"].startswith("The two-family")
    assert record["remaining_delta"].startswith("Prove the standalone")
    assert "why_hard" not in record
    assert "suggested_attack" not in record
    assert difficulty_json_path(declaration_path).is_file()
    assert worker_dir.is_dir()


def test_record_difficulty_rejects_invalid_targeted_child_or_unwitnessed_refutation(tmp_path):
    worker_dir = tmp_path / "prop-worker-self"
    worker_dir.mkdir()
    (worker_dir / "research_target.json").write_text(
        json.dumps({
            "worker_id": "worker-self",
            "difficulty_id": "global-packing",
            "difficulty_statement": "Prove the global packing bound.",
        }),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    register_difficulty_declaration_tool(
        registry,
        declaration_path=worker_dir / "difficulty_declaration.md",
        role="generator",
    )

    self_parent = registry.execute(
        "RecordDifficulty",
        {
            "event_kind": "blocked",
            "source_difficulty_id": "global-packing",
            "blocking_obligation": "Control one unproved local packing inequality.",
            "verified_boundary": "The reduction is proved.",
            "remaining_delta": "A local inequality remains.",
        },
    )
    assert self_parent.is_error
    assert "differ from the assigned parent" in self_parent.content

    repeated_parent = registry.execute(
        "RecordDifficulty",
        {
            "event_kind": "blocked",
            "source_difficulty_id": "packing-reworded",
            "blocking_obligation": "Prove the global packing bound.",
            "verified_boundary": "No smaller boundary was found.",
            "remaining_delta": "The original theorem remains.",
        },
    )
    assert repeated_parent.is_error
    assert "restates the assigned parent" in repeated_parent.content

    root_refutation = registry.execute(
        "RecordDifficulty",
        {
            "event_kind": "refuted",
            "source_difficulty_id": "packing-counterexample",
            "blocking_obligation": "The proposed packing inequality.",
        },
    )
    assert root_refutation.is_error
    assert "refutation_witness is required" in root_refutation.content


def test_materialized_handoff_contains_only_minimal_obstacle_facts(tmp_path):
    worker_dir, declaration_path = _record_generator_difficulty(tmp_path)
    review = worker_dir / "review.md"
    review.write_text("Verdict: fail\nThe global packing step is absent.\n", encoding="utf-8")

    handoff_path, handoff = materialize_difficulty_handoff(
        declaration_path=declaration_path,
        worker_id="worker-a",
        difficulty_id="global-packing",
        method_id="matching",
        execution_status="rejected",
        failure_kind="verification_rejected",
        review_file=review,
        proposition_file=None,
        verified_file=None,
    )

    assert handoff_path is not None and handoff_path.is_file()
    assert handoff is not None
    assert handoff["source_difficulty_id"] == "packing-extra-marker"
    assert handoff["parent_difficulty_id"] == "global-packing"
    assert handoff["event_kind"] == "blocked"
    assert handoff["verified_boundary"].startswith("The two-family")
    assert handoff["remaining_delta"].startswith("Prove the standalone")
    assert handoff["requires_checkpoint_curation"] is True
    assert str(review) in handoff["evidence_refs"]
    assert "why_current_route_fails" not in handoff
    assert "suggested_attack" not in handoff
    assert "dead_ends" not in handoff


def test_portfolio_exposes_minimal_handoff_facts(tmp_path):
    _, declaration_path = _record_generator_difficulty(tmp_path)
    _, handoff = materialize_difficulty_handoff(
        declaration_path=declaration_path,
        worker_id="worker-a",
        difficulty_id="global-packing",
        method_id="matching",
        execution_status="rejected",
        failure_kind="verification_rejected",
        review_file=None,
        proposition_file=None,
        verified_file=None,
    )
    assert handoff is not None
    compact = candidate_handoffs([{"difficulty_handoff": handoff}])[0]
    assert compact["source_difficulty_id"] == "packing-extra-marker"
    assert compact["event_kind"] == "blocked"
    assert compact["blocking_obligation"].startswith("Control the extra marker")
    assert compact["verified_boundary"].startswith("The two-family")
    assert compact["remaining_delta"].startswith("Prove the standalone")
