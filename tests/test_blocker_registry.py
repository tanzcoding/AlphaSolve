import json

import pytest

from alphasolve.agent.tools import ToolRegistry
from alphasolve.solver.difficulty_dag import (
    MAX_PERSISTENT_DIFFICULTY_DEPTH,
    PARENT_DIRECT_ATTACK_OUTCOME_INTERVAL,
    DifficultyDagStore,
    register_curated_difficulty_dag_tool,
)
from alphasolve.solver.project import ProjectLayout


def _checkpoint(layout, checkpoint_id="checkpoint-0002"):
    directory = layout.progress_audits_dir / checkpoint_id
    directory.mkdir(parents=True)
    (directory / "decision.json").write_text(
        json.dumps({"checkpoint_id": checkpoint_id, "status": "completed", "watermark": 2}),
        encoding="utf-8",
    )


def _difficulty(difficulty_id, source_id, *, parents=None, policy="manual", status="open"):
    return {
        "difficulty_id": difficulty_id,
        "statement": f"Resolve {difficulty_id}.",
        "parent_difficulty_ids": parents or [],
        "resolution_policy": policy,
        "relation_to_parent": "prerequisite",
        "source_difficulty_ids": [source_id],
        "evidence_refs": [f"workers/{source_id}/difficulty_handoff.json"],
        "status": status,
    }


def _layout(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return layout


def test_curator_persists_recursive_difficulty_dag_and_leaf_dispatch(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)

    result = dag.record_curation(
        checkpoint_id="checkpoint-0002",
        difficulties=[
            _difficulty("main", "worker-main", policy="all_of"),
            _difficulty("lemma-left", "worker-left", parents=["main"]),
            _difficulty("lemma-right", "worker-right", parents=["main"]),
        ],
    )

    assert result["recorded"] is True
    assert dag.pending_checkpoint_ids() == []
    leaves = dag.selection_snapshot()["executable_difficulties"]
    assert {item["difficulty_id"] for item in leaves} == {"lemma-left", "lemma-right"}
    internal = dag.dispatch_preflight(
        difficulty_id="main", method_id="direct_proof", require_curation=True
    )
    assert internal["allowed"] is True
    assert internal["open_child_difficulty_ids"] == ["lemma-left", "lemma-right"]
    assert "Leaf-first is recommended" in internal["dispatch_warnings"][0]


def test_manual_parent_allows_budgeted_direct_attack_while_children_active(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0002",
        difficulties=[
            _difficulty("terminal", "source-terminal", policy="manual"),
            _difficulty("leaf", "source-leaf", parents=["terminal"]),
        ],
    )

    frontier = dag.selection_snapshot()
    assert {item["difficulty_id"] for item in frontier["executable_difficulties"]} == {"leaf"}
    assert {item["difficulty_id"] for item in frontier["parent_direct_difficulties"]} == {"terminal"}
    internal = dag.dispatch_preflight(
        difficulty_id="terminal", method_id="direct_proof", require_curation=True
    )
    assert internal["allowed"] is True
    assert internal["open_child_difficulty_ids"] == ["leaf"]

    allowed = dag.dispatch_preflight(
        difficulty_id="terminal",
        method_id="direct_proof",
        require_curation=True,
        parent_direct_attack=True,
    )
    assert allowed["allowed"] is True
    assert allowed["difficulty"]["dispatch_mode"] == "parent_direct"
    first_attempt = dag.register_parent_direct_attack(difficulty_id="terminal", worker_id="worker-parent")
    assert first_attempt["within_recommended_interval"] is True
    over_interval = dag.dispatch_preflight(
        difficulty_id="terminal",
        method_id="consolidation",
        require_curation=True,
        parent_direct_attack=True,
    )
    assert over_interval["allowed"] is True
    assert "not yet replenished" in over_interval["dispatch_warnings"][-1]
    second_attempt = dag.register_parent_direct_attack(difficulty_id="terminal", worker_id="worker-parent-again")
    assert second_attempt["within_recommended_interval"] is False
    assert second_attempt["warning"] is not None

    for index in range(PARENT_DIRECT_ATTACK_OUTCOME_INTERVAL):
        dag.record_attack_outcome(
            difficulty_id="leaf",
            worker_id=f"worker-leaf-{index}",
            outcome="unchanged",
            summary="The leaf remains unresolved.",
        )
    assert dag.dispatch_preflight(
        difficulty_id="terminal",
        method_id="consolidation",
        require_curation=True,
        parent_direct_attack=True,
    )["allowed"] is True


def test_refuted_child_does_not_refute_manual_parent_claim(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0002",
        difficulties=[
            _difficulty("terminal", "source-terminal", policy="manual"),
            _difficulty("bad-milestone", "source-milestone", parents=["terminal"]),
        ],
    )

    dag.record_attack_outcome(
        difficulty_id="bad-milestone",
        worker_id="worker-refutation",
        outcome="refuted",
        summary="The proposed intermediate claim has a witness against it.",
    )
    assert dag.load()["nodes"]["terminal"]["status"] == "open"
    assert dag.dispatch_preflight(
        difficulty_id="terminal", method_id="direct_proof", require_curation=True
    )["allowed"] is True


def test_all_of_children_require_explicit_synthesis(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0002",
        difficulties=[
            _difficulty("parent", "source-parent", policy="all_of"),
            _difficulty("left", "source-left", parents=["parent"]),
            _difficulty("right", "source-right", parents=["parent"]),
        ],
    )

    dag.record_attack_outcome(difficulty_id="left", worker_id="worker-left", outcome="resolved", summary="proved left")
    dag.record_attack_outcome(difficulty_id="right", worker_id="worker-right", outcome="resolved", summary="proved right")

    normalized = dag.dispatch_preflight(difficulty_id="parent", method_id="direct_proof", require_curation=True)
    assert normalized["allowed"] is True
    assert normalized["difficulty"]["dispatch_mode"] == "consolidation"
    assert "normalized to fixed-target consolidation" in normalized["dispatch_warnings"][0]
    assert dag.dispatch_preflight(
        difficulty_id="parent", method_id="consolidation", require_curation=True
    )["allowed"] is True


def test_any_of_resolves_parent_and_supersedes_sibling(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0002",
        difficulties=[
            _difficulty("parent", "source-parent", policy="any_of"),
            _difficulty("first", "source-first", parents=["parent"]),
            _difficulty("second", "source-second", parents=["parent"]),
        ],
    )

    dag.record_attack_outcome(difficulty_id="first", worker_id="worker-first", outcome="resolved", summary="proved first")
    state = dag.load()
    assert state["nodes"]["parent"]["status"] == "resolved"
    assert state["nodes"]["second"]["status"] == "superseded"


def test_curation_reuses_existing_alias_and_normalizes_legacy_source_ids(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout, "checkpoint-0002")
    _checkpoint(layout, "checkpoint-0003")
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0002",
        difficulties=[_difficulty("canonical-root", "fooling-2025-universal")],
    )

    result = dag.record_curation(
        checkpoint_id="checkpoint-0003",
        difficulties=[
            _difficulty("stale-reinterpretation", "fooling-2025-universal"),
            _difficulty(
                "legacy-child",
                "fooling-universal/lis-lds-bound",
                parents=["stale-reinterpretation"],
            ),
        ],
    )

    state = dag.load()
    assert result["recorded"] is True
    assert state["aliases"]["fooling-2025-universal"] == "canonical-root"
    assert state["aliases"]["fooling-universal-lis-lds-bound"] == "legacy-child"
    assert state["nodes"]["legacy-child"]["parent_ids"] == ["canonical-root"]
    assert "stale-reinterpretation" not in state["nodes"]
    assert dag.is_checkpoint_curated("checkpoint-0003")


def test_curation_rejects_persistent_difficulty_depth_over_three(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)
    difficulties = [_difficulty("root", "source-root")]
    parent = "root"
    for depth in range(1, MAX_PERSISTENT_DIFFICULTY_DEPTH + 2):
        child = f"depth-{depth}"
        difficulties.append(_difficulty(child, f"source-{child}", parents=[parent]))
        parent = child

    with pytest.raises(ValueError, match="persistent difficulty depth exceeds"):
        dag.record_curation(checkpoint_id="checkpoint-0002", difficulties=difficulties)


def test_curator_tool_rejects_cycles_and_preserves_source_aliases(tmp_path):
    layout = _layout(tmp_path)
    _checkpoint(layout)
    dag = DifficultyDagStore(layout.workspace_dir)
    tools = ToolRegistry()
    register_curated_difficulty_dag_tool(tools, difficulty_dag=dag, checkpoint_id="checkpoint-0002")

    rejected = tools.execute(
        "CurateDifficultyDag",
        {"difficulties": [
            _difficulty("a", "source-a", parents=["b"]),
            _difficulty("b", "source-b", parents=["a"]),
        ]},
    )
    assert rejected.is_error

    recorded = tools.execute("CurateDifficultyDag", {"difficulties": [_difficulty("a", "source-a")]})
    assert not recorded.is_error
    assert dag.load()["aliases"]["source-a"] == "a"
