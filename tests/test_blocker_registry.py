import json

from alphasolve.agent.tools import ToolRegistry
from alphasolve.solver.blocker_registry import (
    PersistentBlockerRegistry,
    register_curated_blocker_registry_tool,
)
from alphasolve.solver.curator import _portfolio_checkpoint_prompt
from alphasolve.solver.project import ProjectLayout


def _checkpoint(layout, checkpoint_id="checkpoint-0002", *, watermark=3):
    checkpoint_dir = layout.progress_audits_dir / checkpoint_id
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "decision.json").write_text(
        json.dumps({"checkpoint_id": checkpoint_id, "status": "completed", "watermark": watermark}),
        encoding="utf-8",
    )
    (checkpoint_dir / "curator_brief.md").write_text("# Brief\n", encoding="utf-8")
    outcomes = [
        {"sequence": 1, "direction_id": "route-a", "gap_id": "gap-a", "method_id": "counting"},
        {"sequence": 2, "direction_id": "route-b", "gap_id": "gap-b", "method_id": "induction"},
        {"sequence": 3, "direction_id": "route-a", "gap_id": "gap-a", "method_id": "counting"},
    ]
    layout.progress_audit_outcomes_path.write_text(
        "".join(json.dumps(item) + "\n" for item in outcomes), encoding="utf-8"
    )


def _current_difficulties(blocker_id="global-cross-term"):
    return [{
        "difficulty_id": "cross-term-obligation",
        "statement": "Control the cross term without assuming the target.",
        "gate": {"direction_id": "route-a", "gap_id": "cross-term"},
        "blocker_id": blocker_id,
    }]


def _blocker_payload():
    return [{
        "blocker_id": "global-cross-term",
        "statement": "Control the cross term without assuming the target.",
        "gate": {"direction_id": "route-a", "gap_id": "cross-term"},
        "approaches": [
            {
                "direction_id": "route-a",
                "gap_id": "gap-a",
                "method_id": "counting",
                "description": "Global counting leaves the cross term uncontrolled.",
                "outcome_sequences": [1, 3],
            },
            {
                "direction_id": "route-b",
                "gap_id": "gap-b",
                "method_id": "induction",
                "description": "Inductive decomposition leaves the same term uncontrolled.",
                "outcome_sequences": [2],
            },
        ],
    }]


def test_curator_blocker_registry_persists_cross_route_occurrences(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)

    registry = PersistentBlockerRegistry(layout.workspace_dir)
    assert registry.pending_checkpoint_ids() == ["checkpoint-0002"]
    result = registry.record_curation(
        checkpoint_id="checkpoint-0002",
        blockers=_blocker_payload(),
        resolved_blocker_ids=[],
        current_difficulties=_current_difficulties(),
        blocker_relations=[],
    )

    blocker = result["active_blockers"][0]
    assert blocker["occurrence_count"] == 3
    assert {item["method_id"] for item in blocker["approaches"]} == {"counting", "induction"}
    assert registry.pending_checkpoint_ids() == []

    restarted = PersistentBlockerRegistry(layout.workspace_dir)
    active = restarted.active_for_direction("route-a")
    assert active[0]["outcome_sequences"] == [1, 2, 3]
    assert restarted.dispatch_preflight(
        direction_id="route-a",
        gap_id="side-lemma",
        require_curation=True,
    )["reason"] == "active_curated_blocker"
    assert restarted.dispatch_preflight(
        direction_id="route-a",
        gap_id="cross-term",
        require_curation=True,
    )["allowed"] is True


def test_curator_retry_prompt_includes_non_persistence_feedback(tmp_path):
    brief = tmp_path / "progress_audits" / "checkpoint-0002" / "curator_brief.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# Brief\n", encoding="utf-8")

    prompt = _portfolio_checkpoint_prompt(
        brief,
        recovery_reason="curator did not persist blocker curation for checkpoint checkpoint-0002",
    )

    assert "# Recovery feedback" in prompt
    assert "did not persist blocker curation" in prompt
    assert "never use `direction/gap`" in prompt
    assert "do not submit a second variant" in prompt


def test_curator_persists_audit_candidate_with_slug_id_and_route_gate(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    decision_path = layout.progress_audits_dir / "checkpoint-0002" / "decision.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["repeated_avoided_obligation"] = {
        "direction_id": "route-a",
        "gap_id": "cross-term",
        "statement": "Control the cross term without assuming the target.",
    }
    decision_path.write_text(json.dumps(decision), encoding="utf-8")

    difficulties = _current_difficulties()
    difficulties[0]["difficulty_id"] = "audit-route-a-cross-term"
    difficulties[0]["statement"] = "Prove the same cross-term control obligation."
    registry = PersistentBlockerRegistry(layout.workspace_dir)
    result = registry.record_curation(
        checkpoint_id="checkpoint-0002",
        blockers=_blocker_payload(),
        resolved_blocker_ids=[],
        current_difficulties=difficulties,
        blocker_relations=[],
    )

    assert result["recorded"] is True
    assert registry.pending_checkpoint_ids() == []
    persisted = registry.load()["curated_checkpoints"]["checkpoint-0002"]
    assert persisted["current_difficulties"][0]["difficulty_id"] == "audit-route-a-cross-term"
    assert persisted["current_difficulties"][0]["gate"] == {"direction_id": "route-a", "gap_id": "cross-term"}


def test_curator_rejects_route_path_difficulty_id_with_actionable_remediation(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)

    invalid = _current_difficulties()
    invalid[0]["difficulty_id"] = "route-a/cross-term"
    try:
        PersistentBlockerRegistry(layout.workspace_dir).record_curation(
            checkpoint_id="checkpoint-0002",
            blockers=_blocker_payload(),
            resolved_blocker_ids=[],
            current_difficulties=invalid,
            blocker_relations=[],
        )
    except ValueError as exc:
        message = str(exc)
        assert "stable identifier" in message
        assert "direction/gap" in message
        assert "gate" in message
    else:
        raise AssertionError("a route path must not be accepted as difficulty_id")



def test_curator_registry_preserves_structured_difficulty_evidence(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    outcomes = [json.loads(line) for line in layout.progress_audit_outcomes_path.read_text(encoding="utf-8").splitlines()]
    outcomes[0]["difficulty_handoff"] = {
        "worker_id": "worker-a",
        "disposition": "active_candidate",
        "blocking_obligation": "Control the cross term without assuming the target.",
        "last_verified_step": "The local count is established.",
        "why_current_route_fails": "The cross term is uncontrolled.",
        "suggested_attack": "Construct a cross-term invariant.",
        "evidence_refs": ["prop-a/review.md"],
    }
    layout.progress_audit_outcomes_path.write_text(
        "".join(json.dumps(item) + "\n" for item in outcomes), encoding="utf-8"
    )

    result = PersistentBlockerRegistry(layout.workspace_dir).record_curation(
        checkpoint_id="checkpoint-0002",
        blockers=_blocker_payload(),
        resolved_blocker_ids=[],
        current_difficulties=_current_difficulties(),
        blocker_relations=[],
    )

    evidence = result["active_blockers"][0]["approaches"][0]["difficulty_handoffs"]
    assert evidence[0]["sequence"] == 1
    assert evidence[0]["blocking_obligation"].startswith("Control the cross term")


def test_curator_registry_blocks_targeted_work_until_completed_checkpoint_is_curated(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout, checkpoint_id="checkpoint-0003", watermark=2)

    restarted = PersistentBlockerRegistry(layout.workspace_dir)
    preflight = restarted.dispatch_preflight(
        direction_id="route-a",
        gap_id="gap-a",
        require_curation=True,
    )

    assert preflight["allowed"] is False
    assert preflight["reason"] == "blocker_curation_pending"
    assert preflight["pending_checkpoints"] == ["checkpoint-0003"]


def test_curator_registry_tool_derives_counts_and_rejects_wrong_provenance(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    blocker_registry = PersistentBlockerRegistry(layout.workspace_dir)
    tools = ToolRegistry()
    register_curated_blocker_registry_tool(
        tools,
        blocker_registry=blocker_registry,
        checkpoint_id="checkpoint-0002",
    )

    wrong = _blocker_payload()
    wrong[0]["approaches"][1]["method_id"] = "falsification"
    rejected = tools.execute(
        "CuratePersistentBlockers",
        {"blockers": wrong, "current_difficulties": _current_difficulties(), "blocker_relations": []},
    )
    assert rejected.is_error

    recorded = tools.execute(
        "CuratePersistentBlockers",
        {"blockers": _blocker_payload(), "current_difficulties": _current_difficulties(), "blocker_relations": []},
    )
    assert not recorded.is_error
    assert json.loads(recorded.content)["active_blockers"][0]["occurrence_count"] == 3


def test_curator_same_relation_merges_duplicate_blockers_across_routes(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    registry = PersistentBlockerRegistry(layout.workspace_dir)
    registry.record_curation(
        checkpoint_id="checkpoint-0002",
        blockers=_blocker_payload(),
        resolved_blocker_ids=[],
        current_difficulties=_current_difficulties(),
        blocker_relations=[],
    )

    state = registry.load()
    alias = json.loads(json.dumps(state["blockers"]["global-cross-term"]))
    alias["blocker_id"] = "cross-term-alias"
    state["blockers"]["cross-term-alias"] = alias
    registry.path.write_text(json.dumps(state), encoding="utf-8")
    _checkpoint(layout, checkpoint_id="checkpoint-0003")

    result = registry.record_curation(
        checkpoint_id="checkpoint-0003",
        blockers=_blocker_payload(),
        resolved_blocker_ids=[],
        current_difficulties=_current_difficulties("global-cross-term"),
        blocker_relations=[
            {
                "difficulty_id": "cross-term-obligation",
                "active_blocker_id": "global-cross-term",
                "relation": "same",
                "canonical_blocker_id": "global-cross-term",
            },
            {
                "difficulty_id": "cross-term-obligation",
                "active_blocker_id": "cross-term-alias",
                "relation": "same",
                "canonical_blocker_id": "global-cross-term",
            },
        ],
    )

    assert result["merged_blocker_ids"] == ["cross-term-alias"]
    after = registry.load()
    assert after["blockers"]["cross-term-alias"]["status"] == "merged"
    assert after["blockers"]["cross-term-alias"]["merged_into"] == "global-cross-term"
    checkpoint = after["curated_checkpoints"]["checkpoint-0003"]
    assert checkpoint["protocol_version"] == 2
    assert {item["relation"] for item in checkpoint["blocker_relations"]} == {"same"}


def test_curator_must_classify_every_active_blocker_and_reconcile_legacy_registry(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    registry = PersistentBlockerRegistry(layout.workspace_dir)
    registry.record_curation(
        checkpoint_id="checkpoint-0002",
        blockers=_blocker_payload(),
        resolved_blocker_ids=[],
        current_difficulties=_current_difficulties(),
        blocker_relations=[],
    )
    _checkpoint(layout, checkpoint_id="checkpoint-0003")

    try:
        registry.record_curation(
            checkpoint_id="checkpoint-0003",
            blockers=_blocker_payload(),
            resolved_blocker_ids=[],
            current_difficulties=_current_difficulties(),
            blocker_relations=[],
        )
    except ValueError as exc:
        assert "must classify every current difficulty" in str(exc)
    else:
        raise AssertionError("curator must not omit an active blocker comparison")

    legacy = registry.load()
    legacy["schema_version"] = 1
    legacy["curated_checkpoints"]["checkpoint-0002"].pop("protocol_version")
    registry.path.write_text(json.dumps(legacy), encoding="utf-8")
    restarted = PersistentBlockerRegistry(layout.workspace_dir)
    assert restarted.load()["reconciliation"]["status"] == "pending"
    assert "checkpoint-0002" in restarted.pending_checkpoint_ids()
