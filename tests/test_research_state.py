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


def test_reviewer_route_lifecycle_and_curated_learning(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_direction()]})
    snapshot = store.reviewer_snapshot()

    registered = store.register_route(
        route_id="route-extension",
        based_on_state_id=snapshot["state_id"],
        direction_id="D1",
        gap_id="D1-G1",
        route_claim="The extension obstruction is the best supported path.",
        target="Prove or refute the extension lemma.",
        success_condition="The terminal gap is closed or explicitly refuted.",
        stop_condition="Distinct attempts hit the same unavoidable counterexample.",
        evidence_refs=["verified_propositions/extension-base.md"],
    )
    assert registered["registered"] is True

    store.attach_worker_to_route(
        route_id="route-extension",
        worker_id="worker-1",
        direction_id="D1",
        gap_id="D1-G1",
        method_id="direct_proof",
    )
    store.record_impact(
        worker_id="worker-1",
        direction_id="D1",
        gap_id="D1-G1",
        route_id="route-extension",
        impact={
            "relation_to_target": "direct_advance",
            "gap_effect": "advanced",
            "continuation_value": "high",
            "summary": "The extension lemma now holds in the critical case.",
        },
    )
    store.assess_route(
        route_id="route-extension",
        verdict="ADVANCING",
        summary="This route narrowed the terminal gap.",
        evidence_paths=["verified_propositions/extension-critical.md"],
    )
    store.commit_route_learning(
        learning_id="learning-extension",
        route_ids=["route-extension"],
        finding="Continue this route only through the remaining boundary case.",
        confidence="medium",
        evidence_paths=["progress_audits/checkpoint-0001/evidence.md"],
        policy_effect="Prefer a boundary-case worker before opening another local lemma.",
    )

    state = store.load()
    route = state["routes"]["route-extension"]
    assert route["worker_ids"] == ["worker-1"]
    assert route["outcomes"][0]["relation_to_target"] == "direct_advance"
    assert route["assessments"][-1]["verdict"] == "ADVANCING"
    assert state["route_learnings"][0]["learning_id"] == "learning-extension"
    assert store.reviewer_snapshot()["route_learnings"][0]["policy_effect"].startswith("Prefer")
    markdown = store.markdown_path.read_text(encoding="utf-8")
    assert "Research Routes" in markdown
    assert "Curated Route Learnings" in markdown


def test_register_route_rejects_stale_reviewer_state(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_direction()]})
    stale_id = store.reviewer_snapshot()["state_id"]
    store.record_method_failure(
        direction_id="D1",
        method_family="counting",
        failure_type="method_blocked",
        summary="The estimate is too weak.",
    )

    try:
        store.register_route(
            route_id="stale-route",
            based_on_state_id=stale_id,
            direction_id="D1",
            gap_id="D1-G1",
            route_claim="Use the stale plan.",
            target="Close the old target.",
            success_condition="Close it.",
            stop_condition="A counterexample appears.",
        )
    except ValueError as exc:
        assert "stale state" in str(exc)
    else:
        raise AssertionError("a route must bind the current canonical state")


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


def _tree_direction(policy: str = "all_of") -> dict:
    return {
        "direction_id": "D1",
        "title": "Recursive route",
        "goal": "Close a recursively decomposed obligation.",
        "status": "active",
        "health": "unassessed",
        "steps": [
            {
                "step_id": "root",
                "statement": "Prove the terminal theorem from its prerequisites.",
                "status": "open",
                "resolution_policy": policy,
                "gap_kind": "terminal",
            },
            {
                "step_id": "lemma-a",
                "statement": "Prove the first prerequisite.",
                "parent_gap_id": "root",
                "status": "open",
                "resolution_policy": "all_of",
                "gap_kind": "lemma",
            },
            {
                "step_id": "lemma-b",
                "statement": "Prove the second prerequisite.",
                "parent_gap_id": "root",
                "status": "open",
                "resolution_policy": "manual",
                "gap_kind": "lemma",
            },
        ],
    }


def _close_gap(store: ResearchStateStore, worker_id: str, gap_id: str) -> None:
    store.record_impact(
        worker_id=worker_id,
        direction_id="D1",
        gap_id=gap_id,
        impact={
            "relation_to_target": "direct_advance",
            "gap_effect": "closed",
            "continuation_value": "high",
            "summary": f"Verified {gap_id}.",
        },
    )


def test_record_impact_creates_child_gaps_and_preserves_parent_policy(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_direction()]})

    store.record_impact(
        worker_id="worker-decompose",
        direction_id="D1",
        gap_id="D1-G1",
        impact={
            "relation_to_target": "direct_advance",
            "gap_effect": "advanced",
            "continuation_value": "high",
            "summary": "The terminal claim decomposes into two independent lemmas.",
            "child_resolution_policy": "all_of",
            "new_gaps": [
                {"gap_id": "lemma-left", "statement": "Prove the left lemma.", "gap_kind": "lemma"},
                {"gap_id": "lemma-right", "statement": "Prove the right lemma.", "gap_kind": "lemma"},
            ],
        },
    )

    steps = store.load()["directions"]["D1"]["steps"]
    assert steps["D1-G1"]["resolution_policy"] == "all_of"
    assert steps["D1-G1"]["child_gap_ids"] == ["lemma-left", "lemma-right"]
    assert steps["lemma-left"]["parent_gap_id"] == "D1-G1"
    assert steps["lemma-right"]["parent_gap_id"] == "D1-G1"


def test_all_of_children_require_parent_synthesis(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_tree_direction("all_of")]})

    _close_gap(store, "worker-a", "lemma-a")
    _close_gap(store, "worker-b", "lemma-b")

    steps = store.load()["directions"]["D1"]["steps"]
    assert steps["root"]["status"] == "advanced"
    assert steps["root"]["ready_for_synthesis"] is True
    assert store.dispatch_preflight(direction_id="D1", gap_id="root", method_id="direct_proof")["reason"] == "gap_synthesis_required"
    assert store.dispatch_preflight(direction_id="D1", gap_id="root", method_id="consolidation")["allowed"] is True

    _close_gap(store, "worker-synthesis", "root")
    assert store.load()["directions"]["D1"]["steps"]["root"]["status"] == "closed"


def test_any_of_closes_parent_and_supersedes_siblings(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_tree_direction("any_of")]})

    _close_gap(store, "worker-a", "lemma-a")

    steps = store.load()["directions"]["D1"]["steps"]
    assert steps["root"]["status"] == "closed"
    assert steps["lemma-b"]["status"] == "superseded"
    blocked = store.dispatch_preflight(direction_id="D1", gap_id="lemma-b", method_id="direct_proof")
    assert blocked["allowed"] is False
    assert blocked["reason"] == "gap_not_schedulable"


def test_route_can_dispatch_a_descendant_gap(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_tree_direction()]})
    route = store.register_route(
        route_id="route-root",
        based_on_state_id=store.reviewer_snapshot()["state_id"],
        direction_id="D1",
        gap_id="root",
        route_claim="Decompose and solve the root obligation.",
        target="Close root.",
        success_condition="A proof closes root.",
        stop_condition="A witness refutes root.",
    )
    assert route["route"]["scope_gap_id"] == "root"
    store.attach_worker_to_route(
        route_id="route-root",
        worker_id="worker-leaf",
        direction_id="D1",
        gap_id="lemma-a",
        method_id="direct_proof",
    )
    assert store.load()["routes"]["route-root"]["worker_ids"] == ["worker-leaf"]


def test_child_solve_or_refutation_does_not_prematurely_finish_direction(tmp_path):
    store = ResearchStateStore(tmp_path / "verified_propositions")
    store.sync({"objective_summary": "Goal.", "directions": [_tree_direction("all_of")]})

    store.record_impact(
        worker_id="worker-solves-child",
        direction_id="D1",
        gap_id="lemma-a",
        impact={
            "relation_to_target": "solves_target",
            "gap_effect": "closed",
            "continuation_value": "high",
            "summary": "Lemma A is proved.",
        },
    )
    assert store.load()["directions"]["D1"]["status"] == "active"
    assert store.dispatch_preflight(direction_id="D1", gap_id="lemma-b", method_id="direct_proof")["allowed"] is True

    store.sync({"objective_summary": "Goal.", "directions": [_tree_direction("any_of")]})
    store.record_impact(
        worker_id="worker-refutes-option",
        direction_id="D1",
        gap_id="lemma-a",
        impact={
            "relation_to_target": "refutes_target",
            "gap_effect": "invalidated",
            "continuation_value": "medium",
            "summary": "The first alternative has a counterexample.",
        },
    )
    assert store.load()["directions"]["D1"]["status"] == "active"
    assert store.dispatch_preflight(direction_id="D1", gap_id="lemma-b", method_id="direct_proof")["allowed"] is True


def test_load_migrates_v1_gap_list_and_sync_preserves_global_baseline(tmp_path):
    verified = tmp_path / "verified_propositions"
    verified.mkdir()
    canonical = {
        "schema_version": 1,
        "state_revision": 3,
        "state_id": "state-3",
        "objective": {"summary": "Goal.", "status": "active"},
        "directions": {
            "D1": {
                "direction_id": "D1",
                "title": "Legacy route",
                "goal": "Close legacy gap.",
                "status": "active",
                "health": "unassessed",
                "gaps": [{"gap_id": "legacy-gap", "statement": "Prove legacy claim.", "status": "open"}],
                "plan": [],
            }
        },
        "global_consolidation": {"last_verified_count": 7, "last_worker_id": "old-worker"},
    }
    (verified / "research_state.json").write_text(json.dumps(canonical), encoding="utf-8")
    store = ResearchStateStore(verified)

    state = store.load()
    assert state["schema_version"] == 2
    assert "legacy-gap" in state["directions"]["D1"]["steps"]
    assert store.default_target() == ("D1", "legacy-gap")

    store.sync({"objective_summary": "Goal.", "directions": [_direction("D1")]})
    baseline = store.load()["global_consolidation"]
    assert baseline["last_verified_count"] == 7
    assert baseline["last_worker_id"] == "old-worker"
