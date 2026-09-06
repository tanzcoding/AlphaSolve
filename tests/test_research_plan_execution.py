from __future__ import annotations

import json

from alphasolve.solver.orchestrator import Orchestrator


def _plan() -> dict[str, object]:
    return {
        "plan_id": "plan-test",
        "frontier_revision": "frontier-r1",
        "recommendation": {
            "research_plan": {
                "objective": "Close the cubic bound.",
                "strategy": "Keep one primary route and one independent challenger.",
                "tracks": [
                    {
                        "track_id": "primary-route",
                        "priority": "primary",
                        "kind": "TARGET_NODE",
                        "difficulty_id": "leaf",
                        "method_id": "contradiction",
                        "route_label": "exact-variance",
                        "research_goal": "Use full interval variance to settle the cubic bound.",
                        "route_contract": {
                            "hypothesis": "Full interval variance carries information absent from the gap relaxation.",
                            "required_invariants": ["Preserve the full interval constraint."],
                            "success_condition": "A proof or refutation of the cubic bound through full interval variance.",
                            "failure_condition": "A witness showing the full interval mechanism cannot control the bound."
                        },
                        "milestones": [{
                            "milestone_id": "three-valued-case",
                            "objective": "Settle the finite three-valued exact-variance case.",
                            "evidence_needed": "A proof or a fully checked counterexample for that finite class."
                        }],
                        "rationale": "It avoids the refuted gap relaxation.",
                        "avoid": "Do not use the two-point gap relaxation.",
                    },
                    {
                        "track_id": "challenger-route",
                        "priority": "challenger",
                        "kind": "NEW_DIRECTION",
                        "difficulty_id": "",
                        "method_id": "falsification",
                        "route_label": "full-interval-counterexample",
                        "research_goal": "Test the cubic bound by searching for an admissible witness.",
                        "route_contract": {
                            "hypothesis": "A finite admissible witness can falsify the target independently of the primary route.",
                            "required_invariants": ["The witness must satisfy all original constraints."],
                            "success_condition": "A valid witness or an exhaustive negative result for the stated finite class.",
                            "failure_condition": "The finite search space is insufficient to discriminate the route."
                        },
                        "milestones": [{
                            "milestone_id": "finite-witness-search",
                            "objective": "Search the stated finite admissible class for a counterexample.",
                            "evidence_needed": "An exact witness or an exhaustive certified negative scan."
                        }],
                        "rationale": "A witness would decide the primary target.",
                        "avoid": "Do not use prefix-only relaxations.",
                    },
                ],
                "hold_reason": "",
            },
            "research_strategy": "Keep one primary route and one independent challenger.",
        },
    }


def _orchestrator() -> tuple[Orchestrator, list[dict[str, object]]]:
    orchestrator = object.__new__(Orchestrator)
    spawned: list[dict[str, object]] = []
    orchestrator._research_plans = {"plan-test": _plan()}
    orchestrator._active_research_plan = None

    class Gateway:
        def validate_frontier_revision(self, *, frontier_revision: str):
            assert frontier_revision == "frontier-r1"
            return {"allowed": True}

    orchestrator._reviewer_plan_gateway = Gateway()

    def spawn(manager, args):
        manager.active += 1
        spawned.append(dict(args))
        return type("Result", (), {"content": json.dumps({"spawned": True, "worker_id": f"worker-{len(spawned)}"})})()

    orchestrator._spawn_difficulty_leaf = spawn
    return orchestrator, spawned


def test_execute_research_plan_dispatches_multiple_orchestrator_sized_tasks():
    orchestrator, spawned = _orchestrator()

    class Manager:
        def __init__(self) -> None:
            self.active = 0

        def has_available_worker_slot(self) -> bool:
            return self.active < 2

    result = orchestrator._execute_research_plan_tool(
        Manager(),
        {
            "plan_id": "plan-test",
            "tasks": [
                {
                    "track_id": "primary-route",
                    "milestone_id": "three-valued-case",
                    "task": "Prove the finite three-valued exact-variance case.",
                    "rubric": "- The Statement covers every three-valued step function.\n- The Statement produces an interval with variance above one.",
                },
                {
                    "track_id": "challenger-route",
                    "milestone_id": "finite-witness-search",
                    "task": "Construct and exactly verify a finite admissible counterexample, if one exists.",
                    "rubric": "- The Statement specifies the witness.\n- The Statement proves its moments, interval variance bound, and cubic violation.",
                },
            ],
        },
    )
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["spawned_worker_ids"] == ["worker-1", "worker-2"]
    assert [item["route_label"] for item in spawned] == ["exact-variance", "full-interval-counterexample"]
    assert [item["research_plan_id"] for item in spawned] == ["plan-test", "plan-test"]
    assert [item["research_track_id"] for item in spawned] == ["primary-route", "challenger-route"]
    assert [item["research_milestone_id"] for item in spawned] == ["three-valued-case", "finite-witness-search"]
    assert [item["track_priority"] for item in spawned] == ["primary", "challenger"]
    assert [item["pinned_target"] for item in spawned] == [
        "Use full interval variance to settle the cubic bound.",
        "Test the cubic bound by searching for an admissible witness.",
    ]
    assert "Prove the finite three-valued exact-variance case." in spawned[0]["hint"]
    assert "Research track" in spawned[0]["hint"]
    assert orchestrator._research_plans["plan-test"]["execution_status"] == "executed"


def test_execute_research_plan_rejects_noncurrent_milestone():
    orchestrator, _spawned = _orchestrator()

    class Manager:
        def has_available_worker_slot(self) -> bool:
            return True

    result = orchestrator._execute_research_plan_tool(
        Manager(),
        {
            "plan_id": "plan-test",
            "tasks": [{
                "track_id": "primary-route",
                "milestone_id": "skipped-stage",
                "task": "Skip to a later route stage.",
                "rubric": "- The Statement proves the skipped stage.",
            }],
        },
    )

    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "task must target the track's current reviewer milestone"
    assert payload["expected_milestone_id"] == "three-valued-case"


def test_execute_research_plan_rejects_duplicate_or_unknown_track():
    orchestrator, _spawned = _orchestrator()

    class Manager:
        def has_available_worker_slot(self) -> bool:
            return True

    duplicate = orchestrator._execute_research_plan_tool(
        Manager(),
        {
            "plan_id": "plan-test",
            "tasks": [
                {"track_id": "primary-route", "milestone_id": "three-valued-case", "task": "First artifact.", "rubric": "- First."},
                {"track_id": "primary-route", "milestone_id": "three-valued-case", "task": "Second artifact.", "rubric": "- Second."},
            ],
        },
    )
    assert duplicate.is_error
    assert json.loads(duplicate.content)["error"] == "a research track may be dispatched once per plan"

    unknown = orchestrator._execute_research_plan_tool(
        Manager(),
        {
            "plan_id": "plan-test",
            "tasks": [{"track_id": "not-in-plan", "milestone_id": "unknown", "task": "Task.", "rubric": "- Criterion."}],
        },
    )
    assert unknown.is_error
    assert json.loads(unknown.content)["error"] == "task references a track not present in the research plan"


def test_busy_worker_pool_keeps_research_plan_retryable():
    orchestrator, _spawned = _orchestrator()

    class BusyManager:
        def has_available_worker_slot(self) -> bool:
            return False

    result = orchestrator._execute_research_plan_tool(
        BusyManager(),
        {"plan_id": "plan-test", "tasks": [{"track_id": "primary-route", "milestone_id": "three-valued-case", "task": "Task.", "rubric": "- Criterion."}]},
    )
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["reason"] == "no_available_worker_slot"
    assert payload["executed"] is False
    assert "execution_status" not in orchestrator._research_plans["plan-test"]


def test_execute_research_plan_persists_hold_without_fake_worker_task():
    orchestrator, _spawned = _orchestrator()
    plan = _plan()
    plan["recommendation"]["research_plan"]["tracks"] = []
    plan["recommendation"]["research_plan"]["hold_reason"] = "No route has an evidence-backed next attack."
    orchestrator._research_plans = {"plan-test": plan}

    class Manager:
        def has_available_worker_slot(self) -> bool:
            return True

    result = orchestrator._execute_research_plan_tool(Manager(), {"plan_id": "plan-test", "tasks": []})
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["reason"] == "research_plan_hold"
    assert orchestrator._research_plans["plan-test"]["execution_status"] == "held"
    assert orchestrator._research_plans["plan-test"]["hold_reason"].startswith("No route")


def test_partially_executed_plan_keeps_unspawned_track_retryable():
    orchestrator, spawned = _orchestrator()

    class Manager:
        def __init__(self) -> None:
            self.active = 0

        def has_available_worker_slot(self) -> bool:
            return self.active < 1

    manager = Manager()
    first = orchestrator._execute_research_plan_tool(
        manager,
        {
            "plan_id": "plan-test",
            "tasks": [
                {"track_id": "primary-route", "milestone_id": "three-valued-case", "task": "Primary artifact.", "rubric": "- Primary."},
                {"track_id": "challenger-route", "milestone_id": "finite-witness-search", "task": "Challenger artifact.", "rubric": "- Challenger."},
            ],
        },
    )
    first_payload = json.loads(first.content)

    assert first_payload["execution_status"] == "partially_executed"
    assert first_payload["pending_track_ids"] == ["challenger-route"]
    assert orchestrator._research_plans["plan-test"]["execution_status"] == "partially_executed"

    manager.active = 0
    second = orchestrator._execute_research_plan_tool(
        manager,
        {"plan_id": "plan-test", "tasks": [{"track_id": "challenger-route", "milestone_id": "finite-witness-search", "task": "Challenger artifact.", "rubric": "- Challenger."}]},
    )
    second_payload = json.loads(second.content)

    assert second_payload["execution_status"] == "executed"
    assert second_payload["pending_track_ids"] == []
    assert [item["research_track_id"] for item in spawned] == ["primary-route", "challenger-route"]


def test_execute_research_plan_requires_fresh_review_after_milestone_contradiction():
    orchestrator, _spawned = _orchestrator()
    orchestrator._research_plans["plan-test"]["reassessment_required"] = True
    orchestrator._research_plans["plan-test"]["reassessment_reason"] = "Worker refuted the current coupling premise."

    class Manager:
        def has_available_worker_slot(self) -> bool:
            return True

    result = orchestrator._execute_research_plan_tool(
        Manager(),
        {
            "plan_id": "plan-test",
            "tasks": [{
                "track_id": "primary-route",
                "milestone_id": "three-valued-case",
                "task": "Continue the old route.",
                "rubric": "- The Statement completes the route.",
            }],
        },
    )

    assert result.is_error
    payload = json.loads(result.content)
    assert payload["error"] == "research plan requires fresh reviewer review"
    assert "refuted" in payload["reason"]


def test_execute_research_plan_rebases_after_unrelated_frontier_update():
    orchestrator, spawned = _orchestrator()

    class RebaseGateway:
        def revalidate_plan_targets(self, *, frontier_revision, target_snapshot, selected_tracks):
            assert frontier_revision == "frontier-r1"
            assert target_snapshot is None  # Legacy plan fixture remains safe for this mock.
            assert [track["track_id"] for track in selected_tracks] == ["primary-route"]
            return {
                "allowed": True,
                "revalidation": "rebased_unaffected",
                "frontier_revision": "frontier-r2",
            }

    class Manager:
        def __init__(self) -> None:
            self.active = 0

        def has_available_worker_slot(self) -> bool:
            return self.active < 1

    orchestrator._reviewer_plan_gateway = RebaseGateway()
    result = orchestrator._execute_research_plan_tool(
        Manager(),
        {"plan_id": "plan-test", "tasks": [{
            "track_id": "primary-route",
            "milestone_id": "three-valued-case",
            "task": "Produce the primary artifact.",
            "rubric": "- The Statement settles the finite case.",
        }]},
    )
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["executed"] is True
    assert len(spawned) == 1
    plan = orchestrator._research_plans["plan-test"]
    assert plan["frontier_revision"] == "frontier-r2"
    assert plan["frontier_revalidations"][0]["result"] == "rebased_unaffected"


def test_execute_research_plan_rejects_stale_or_reused_plan():
    orchestrator, _spawned = _orchestrator()

    class StaleGateway:
        def validate_frontier_revision(self, *, frontier_revision: str):
            return {"allowed": False, "reason": "reviewer_plan_stale", "message": "Refresh."}

    class Manager:
        def has_available_worker_slot(self) -> bool:
            return True

    orchestrator._reviewer_plan_gateway = StaleGateway()
    stale = orchestrator._execute_research_plan_tool(
        Manager(),
        {"plan_id": "plan-test", "tasks": [{"track_id": "primary-route", "milestone_id": "three-valued-case", "task": "Task.", "rubric": "- Criterion."}]},
    )
    stale_payload = json.loads(stale.content)
    assert stale_payload["reason"] == "reviewer_plan_stale"

    orchestrator._research_plans["plan-test"]["execution_status"] = "executed"
    duplicate = orchestrator._execute_research_plan_tool(
        Manager(),
        {"plan_id": "plan-test", "tasks": [{"track_id": "primary-route", "milestone_id": "three-valued-case", "task": "Task.", "rubric": "- Criterion."}]},
    )
    assert duplicate.is_error
    assert json.loads(duplicate.content)["error"] == "research plan has already been executed"


def test_raw_spawn_remains_a_compatible_evidence_collection_path():
    orchestrator, spawned = _orchestrator()
    manager = type("Manager", (), {"active": 0})()
    result = orchestrator._spawn_tool(manager, {"hint": "Check one bounded premise.", "rubric": "- State one checkable claim."})
    payload = json.loads(result.content)

    assert payload["spawned"] is True
    assert spawned[0]["hint"] == "Check one bounded premise."
