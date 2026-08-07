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
            "target_status": "PARTIAL",
            "source_difficulty_id": "packing-extra-marker",
            "difficulty": "Control the extra marker family in the global packing bound.",
            "relation_to_parent": "prerequisite",
            "parent_resolution_policy": "all_of",
            "last_verified_step": "The two-family matching gives an N+1 lower bound.",
            "why_hard": "The present proof controls local matches but not global packing.",
            "suggested_attack": "Analyze the marker conflict graph.",
            "dead_ends": "Repeating the two-family count cannot create the third-family bound.",
        },
    )
    assert not result.is_error
    return worker_dir, declaration_path


def test_record_difficulty_preserves_parent_context_and_recursive_proposal(tmp_path):
    worker_dir, declaration_path = _record_generator_difficulty(tmp_path)
    declaration = load_difficulty_declaration(declaration_path)

    assert declaration is not None
    assert declaration["runtime_context"]["difficulty_id"] == "global-packing"
    assert declaration["generator"]["source_difficulty_id"] == "packing-extra-marker"
    assert declaration["generator"]["parent_difficulty_id"] == "global-packing"
    assert declaration["generator"]["parent_resolution_policy"] == "all_of"
    assert difficulty_json_path(declaration_path).is_file()
    assert worker_dir.is_dir()


def test_materialized_handoff_is_curator_candidate_with_parent_relation(tmp_path):
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
        result_summary_file=None,
        review_file=review,
        proposition_file=None,
        verified_file=None,
    )

    assert handoff_path is not None and handoff_path.is_file()
    assert handoff is not None
    assert handoff["source_difficulty_id"] == "packing-extra-marker"
    assert handoff["parent_difficulty_id"] == "global-packing"
    assert handoff["relation_to_parent"] == "prerequisite"
    assert handoff["requires_checkpoint_curation"] is True
    assert str(review) in handoff["evidence_refs"]


def test_portfolio_exposes_recursive_handoff_provenance(tmp_path):
    _, declaration_path = _record_generator_difficulty(tmp_path)
    _, handoff = materialize_difficulty_handoff(
        declaration_path=declaration_path,
        worker_id="worker-a",
        difficulty_id="global-packing",
        method_id="matching",
        execution_status="rejected",
        failure_kind="verification_rejected",
        result_summary_file=None,
        review_file=None,
        proposition_file=None,
        verified_file=None,
    )
    assert handoff is not None
    compact = candidate_handoffs([{"difficulty_handoff": handoff}])[0]
    assert compact["source_difficulty_id"] == "packing-extra-marker"
    assert compact["parent_difficulty_id"] == "global-packing"
    assert compact["relation_to_parent"] == "prerequisite"
