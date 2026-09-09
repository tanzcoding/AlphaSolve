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


def _worker_dir(tmp_path):
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
    return worker_dir, worker_dir / "difficulty_declaration.md"


def _record_generator_obstacle(tmp_path):
    worker_dir, declaration_path = _worker_dir(tmp_path)
    registry = ToolRegistry()
    register_difficulty_declaration_tool(registry, declaration_path=declaration_path, role="generator")
    result = registry.execute(
        "RecordDifficulty",
        {
            "obstacle": "The current matching argument does not control the extra marker family, so it cannot prove the global packing bound.",
        },
    )
    assert not result.is_error
    return worker_dir, declaration_path


def test_record_difficulty_appends_generator_reviser_and_reasoning_records(tmp_path):
    worker_dir, declaration_path = _worker_dir(tmp_path)
    reasoning = ToolRegistry()
    register_difficulty_declaration_tool(
        reasoning,
        declaration_path=declaration_path,
        role="reasoning_subagent",
        record_context={
            "delegated_description": "Check marker-family lemma",
            "delegated_task": "Prove the marker-family inequality under the current assumptions.",
            "subagent_session_id": "worker-a/reasoning/1",
        },
    )
    generator = ToolRegistry()
    register_difficulty_declaration_tool(generator, declaration_path=declaration_path, role="generator")
    reviser = ToolRegistry()
    register_difficulty_declaration_tool(reviser, declaration_path=declaration_path, role="reviser")

    assert not reasoning.execute("RecordDifficulty", {"obstacle": "The reduction leaves an equality case with no exclusion argument."}).is_error
    assert not generator.execute("RecordDifficulty", {"obstacle": "Without the marker-family lemma, the global packing bound remains unproved."}).is_error
    assert not reviser.execute("RecordDifficulty", {"obstacle": "The verifier's global step cannot be repaired from the current hypotheses."}).is_error

    declaration = load_difficulty_declaration(declaration_path)
    assert declaration is not None
    assert declaration["runtime_context"]["difficulty_id"] == "global-packing"
    records = declaration["records"]
    assert [record["role"] for record in records] == ["reasoning_subagent", "generator", "reviser"]
    assert records[0]["delegated_description"] == "Check marker-family lemma"
    assert records[0]["delegated_task"].startswith("Prove the marker-family")
    assert records[0]["subagent_session_id"] == "worker-a/reasoning/1"
    assert difficulty_json_path(declaration_path).is_file()
    markdown = declaration_path.read_text(encoding="utf-8")
    assert "### Record 1 — reasoning_subagent" in markdown
    assert "### Record 2 — generator" in markdown
    assert "### Record 3 — reviser" in markdown
    assert worker_dir.is_dir()


def test_record_difficulty_requires_a_nonempty_obstacle(tmp_path):
    worker_dir = tmp_path / "prop-worker-empty"
    worker_dir.mkdir()
    registry = ToolRegistry()
    register_difficulty_declaration_tool(
        registry,
        declaration_path=worker_dir / "difficulty_declaration.md",
        role="reasoning_subagent",
    )

    result = registry.execute("RecordDifficulty", {"obstacle": ""})

    assert result.is_error
    assert "obstacle must not be empty" in result.content


def test_materialized_handoff_prefers_outer_obstacle_and_retains_reasoning_evidence(tmp_path):
    worker_dir, declaration_path = _worker_dir(tmp_path)
    reasoning = ToolRegistry()
    register_difficulty_declaration_tool(
        reasoning,
        declaration_path=declaration_path,
        role="reasoning_subagent",
        record_context={
            "delegated_description": "Check marker-family lemma",
            "delegated_task": "Prove the marker-family inequality under the current assumptions.",
        },
    )
    generator = ToolRegistry()
    register_difficulty_declaration_tool(generator, declaration_path=declaration_path, role="generator")
    assert not reasoning.execute("RecordDifficulty", {"obstacle": "The equality case is not excluded."}).is_error
    assert not generator.execute("RecordDifficulty", {"obstacle": "The unresolved equality case prevents proving the global packing bound."}).is_error
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
    assert handoff["handoff_id"] == "handoff-worker-a"
    assert handoff["difficulty_id"] == "global-packing"
    assert handoff["obstacle_source_role"] == "generator"
    assert handoff["obstacle"].startswith("The unresolved equality")
    assert [record["role"] for record in handoff["obstacle_records"]] == ["reasoning_subagent", "generator"]
    assert handoff["obstacle_records"][0]["delegated_description"] == "Check marker-family lemma"
    assert handoff["requires_checkpoint_curation"] is True
    assert str(review) in handoff["evidence_refs"]
    assert "event_kind" not in handoff
    assert "verified_boundary" not in handoff
    assert "remaining_delta" not in handoff
    assert "refutation_witness" not in handoff


def test_handoff_retains_plan_local_causal_context_and_scope_boundary(tmp_path):
    worker_dir, declaration_path = _worker_dir(tmp_path)
    target = json.loads((worker_dir / "research_target.json").read_text(encoding="utf-8"))
    target["causal_context"] = {
        "track_id": "phase-encoding",
        "milestone_id": "controlled-run",
        "causal_intent": "Test whether the candidate run controls phase.",
        "expected_evidence": "A bypass-resistant local construction.",
    }
    (worker_dir / "research_target.json").write_text(json.dumps(target), encoding="utf-8")
    registry = ToolRegistry()
    register_difficulty_declaration_tool(registry, declaration_path=declaration_path, role="generator")
    result = registry.execute(
        "RecordDifficulty",
        {
            "obstacle": "An admissible matching bypasses the candidate run's intended phase.",
            "delivered_instead": "A counterexample for this particular run layout.",
            "obstacle_scope": "local",
            "causal_effect": "Blocks the current layout but not the phase-encoding architecture.",
            "scope_boundary": "The eight-seat candidate run only.",
            "does_not_establish": "Does not refute a different phase-controlled run.",
            "next_blocking_condition": "A bypass-resistant run is still needed.",
        },
    )
    assert not result.is_error

    _, handoff = materialize_difficulty_handoff(
        declaration_path=declaration_path,
        worker_id="worker-a",
        difficulty_id="global-packing",
        method_id="matching",
        execution_status="verified",
        failure_kind=None,
        review_file=None,
        proposition_file=None,
        verified_file=None,
    )

    assert handoff is not None
    assert handoff["causal_context"]["milestone_id"] == "controlled-run"
    assert handoff["causal_effect"].startswith("Blocks the current layout")
    assert handoff["scope_boundary"] == "The eight-seat candidate run only."
    assert handoff["does_not_establish"].startswith("Does not refute")


def test_portfolio_exposes_task_specific_reasoning_observation(tmp_path):
    _, declaration_path = _record_generator_obstacle(tmp_path)
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
    assert compact["handoff_id"] == "handoff-worker-a"
    assert compact["difficulty_id"] == "global-packing"
    assert compact["obstacle"].startswith("The current matching")
    assert compact["obstacle_records"][0]["role"] == "generator"
    assert set(compact).isdisjoint({"event_kind", "verified_boundary", "remaining_delta", "refutation_witness"})
