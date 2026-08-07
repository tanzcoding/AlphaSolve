from __future__ import annotations

import json
from pathlib import Path

import alphasolve.solver as solver_pkg
from alphasolve.agent import load_agent_suite
from alphasolve.solver.curation_records import read_recent_events
from alphasolve.solver.difficulty_dag import DifficultyDagStore
from alphasolve.solver.orchestrator import Orchestrator
from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.worker import WorkerRunResult


class _FakeManager:
    solved_result = None
    solution_path = None

    def __init__(self):
        self.calls = []

    def spawn(self, hint, **kwargs):
        self.calls.append((hint, kwargs))
        return {"spawned": True, "worker_id": "worker-1", "difficulty_id": kwargs.get("difficulty_id")}

    def spawn_free_exploration_if_available(self, *, exploration_constraints):
        self.calls.append(("free", {"exploration_constraints": exploration_constraints}))
        return {"spawned": True, "worker_id": "worker-free", "difficulty_id": None}


def _checkpoint(layout):
    directory = layout.progress_audits_dir / "checkpoint-0001"
    directory.mkdir(parents=True)
    (directory / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed", "watermark": 1}),
        encoding="utf-8",
    )


def _curate_leaf(dag):
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "packing-leaf",
            "statement": "Control the global packing term.",
            "source_difficulty_ids": ["worker-source-packing"],
            "evidence_refs": ["workers/a/difficulty_handoff.json"],
        }],
    )


def _orchestrator(layout):
    orchestrator = object.__new__(Orchestrator)
    orchestrator.layout = layout
    orchestrator.curator_queue = None
    orchestrator.difficulty_dag = DifficultyDagStore(layout.workspace_dir)
    orchestrator._worker_difficulties = {}
    return orchestrator


def test_orchestrator_exposes_difficulty_tools_only():
    config_dir = Path(solver_pkg.__file__).parent / "config"
    suite = load_agent_suite(config_dir)
    tools = list(suite.agents["orchestrator"].tools)
    assert "DifficultyFrontier" in tools
    assert "RecordDifficultyOutcome" in tools
    assert "RegisterResearchRoute" not in tools
    assert "RecordResearchImpact" not in tools
    assert "SyncResearchState" not in tools


def test_spawn_requires_curator_executable_leaf(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    orchestrator = _orchestrator(layout)
    manager = _FakeManager()

    blocked = orchestrator._spawn_tool(manager, {"difficulty_id": "unknown"})
    assert json.loads(blocked.content)["reason"] == "unknown_difficulty"

    _curate_leaf(orchestrator.difficulty_dag)
    spawned = orchestrator._spawn_tool(manager, {"difficulty_id": "packing-leaf", "method_id": "construction"})
    payload = json.loads(spawned.content)
    assert payload["spawned"] is True
    assert orchestrator._worker_difficulties["worker-1"] == "packing-leaf"
    hint, kwargs = manager.calls[0]
    assert "Control the global packing term" in hint
    assert kwargs["difficulty_id"] == "packing-leaf"


def test_spawn_allows_budgeted_direct_parent_attack(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    orchestrator = _orchestrator(layout)
    manager = _FakeManager()
    orchestrator.difficulty_dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[
            {
                "difficulty_id": "terminal",
                "statement": "Prove the exact terminal inequality.",
                "resolution_policy": "manual",
                "source_difficulty_ids": ["source-terminal"],
            },
            {
                "difficulty_id": "leaf",
                "statement": "Prove an intermediate bound.",
                "parent_difficulty_ids": ["terminal"],
                "source_difficulty_ids": ["source-leaf"],
            },
        ],
    )

    spawned = orchestrator._spawn_tool(manager, {
        "difficulty_id": "terminal",
        "parent_direct_attack": True,
        "consolidation": True,
    })
    payload = json.loads(spawned.content)
    assert payload["spawned"] is True
    assert payload["parent_direct_attack"]["registered"] is True
    hint, kwargs = manager.calls[0]
    assert "fixed-target direct attack" in hint
    assert kwargs["difficulty_id"] == "terminal"
    assert kwargs["allow_weakening"] is False
    assert kwargs["pinned_target"] == "Prove the exact terminal inequality."


def test_free_exploration_records_frontier_deviation_and_completion(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    orchestrator = _orchestrator(layout)
    manager = _FakeManager()
    _curate_leaf(orchestrator.difficulty_dag)

    spawned = orchestrator._spawn_free_exploration_tool(manager, {
        "reason": "A dual construction may falsify the common lower-bound heuristic.",
    })

    payload = json.loads(spawned.content)
    assert payload["spawned"] is True
    assert payload["frontier_deviation"]["executable_difficulty_ids"] == ["packing-leaf"]
    assert "A dual construction" in manager.calls[-1][1]["exploration_constraints"]

    result = WorkerRunResult(
        worker_id="worker-free",
        worker_dir=layout.unverified_dir / "prop-worker-free",
        status="rejected",
        summary="The dual construction collapses to the existing obstruction.",
        method_id="free_exploration",
    )
    completion = orchestrator._handle_worker_completion(result)
    assert completion["frontier_deviation"]["status"] == "rejected"
    events = read_recent_events(layout)
    assert [event["kind"] for event in events[-2:]] == [
        "frontier_deviation_started",
        "frontier_deviation_completed",
    ]


def test_outcome_updates_only_the_assigned_difficulty(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    _checkpoint(layout)
    orchestrator = _orchestrator(layout)
    _curate_leaf(orchestrator.difficulty_dag)
    orchestrator._worker_difficulties["worker-1"] = "packing-leaf"

    result = orchestrator._record_difficulty_outcome_tool({
        "worker_id": "worker-1",
        "outcome": "advanced",
        "summary": "A verified bound narrows the packing term.",
    })
    assert not result.is_error
    assert orchestrator.difficulty_dag.load()["nodes"]["packing-leaf"]["status"] == "advanced"
