from __future__ import annotations

import json

from alphasolve.solver.research_state import ResearchStateStore


def _direction(direction_id: str = "D1") -> dict:
    return {
        "direction_id": direction_id,
        "title": "Primary route",
        "goal": "Prove the target.",
        "status": "active",
        "health": "unassessed",
        "steps": [
            {"step_id": "D1-G1", "statement": "Close the terminal gap.", "status": "open"},
        ],
    }


def test_ensure_initialized_creates_canonical_state_and_markdown(tmp_path):
    verified = tmp_path / "verified_propositions"
    verified.mkdir()
    (verified / "state.md").write_text("\n", encoding="utf-8")
    store = ResearchStateStore(verified)

    result = store.ensure_initialized()

    assert result["initialized"] is True
    assert store.json_path.is_file()
    assert store.markdown_path.is_file()
    assert "Verified Propositions State" in store.markdown_path.read_text(encoding="utf-8")


def test_sync_preserves_omitted_directions(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Solve target.", "directions": [_direction("D1")]})
    store.sync({"objective_summary": "Solve target.", "directions": [_direction("D2")]})

    assert set(store.load()["directions"]) == {"D1", "D2"}


def test_record_impact_rejects_weak_relation_closing_gap(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Solve target.", "directions": [_direction()]})

    try:
        store.record_impact(
            worker_id="worker-1",
            direction_id="D1",
            gap_id="D1-G1",
            impact={
                "relation_to_target": "orthogonal",
                "gap_effect": "closed",
                "continuation_value": "low",
                "summary": "Correct but unrelated.",
            },
        )
    except ValueError as exc:
        assert "cannot report" in str(exc)
    else:
        raise AssertionError("weak relation must not close a gap")


def test_tabu_list_rendered_in_state_md(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_direction()]})
    store.record_method_failure(
        direction_id="D1",
        method_family="anchor-graph",
        failure_type="method_blocked",
        summary="Bound too loose.",
    )
    store.record_method_failure(
        direction_id="D1",
        method_family="induction",
        failure_type="conclusion_refuted",
        summary="Counterexample at n=6.",
    )

    markdown = store.markdown_path.read_text(encoding="utf-8")
    assert "Failed methods" in markdown
    assert "anchor-graph" in markdown
    assert "induction" in markdown


def test_state_markdown_projects_direction_attempts_difficulty_and_persistent_blockers(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_direction()]})
    store.attempt_ledger_path.write_text(
        json.dumps({
            "sequence": 7,
            "worker_id": "worker-7",
            "direction_id": "D1",
            "gap_id": "D1-G1",
            "method_id": "matching",
            "status": "rejected",
            "summary": "The global step is missing.",
            "proposition_file": "unverified_propositions/prop-7/proposition.md",
            "difficulty_handoff": {
                "disposition": "active_candidate",
                "blocking_obligation": "Prove the missing global compatibility bound.",
                "last_verified_step": "The local matching lemma is proved.",
                "suggested_attack": "Study the compatibility graph.",
            },
        }) + "\n",
        encoding="utf-8",
    )
    store.blocker_registry_path.parent.mkdir(parents=True, exist_ok=True)
    store.blocker_registry_path.write_text(
        json.dumps({
            "blockers": {
                "global-compatibility": {
                    "blocker_id": "global-compatibility",
                    "status": "active",
                    "statement": "Prove the missing global compatibility bound.",
                    "gate": {"direction_id": "D1", "gap_id": "D1-G1"},
                    "approaches": [{"direction_id": "D1"}],
                    "occurrence_count": 2,
                }
            }
        }),
        encoding="utf-8",
    )

    store.refresh_markdown_view()
    markdown = store.markdown_path.read_text(encoding="utf-8")

    assert "Difficulty Watch" in markdown
    assert "Active Persistent Blockers" in markdown
    assert "Recent worker attempts" in markdown
    assert "Attempt #7" in markdown
    assert "candidate proposition `unverified_propositions/prop-7/proposition.md`" in markdown
    assert "Prove the missing global compatibility bound." in markdown
