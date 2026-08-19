from __future__ import annotations

import json

from alphasolve.solver.difficulty_dag import DifficultyDagStore
from alphasolve.solver.project import ProjectLayout


def _layout(tmp_path):
    (tmp_path / "problem.md").write_text("# Problem\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    checkpoint = layout.progress_audits_dir / "checkpoint-0001"
    checkpoint.mkdir(parents=True)
    (checkpoint / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed", "watermark": 1}),
        encoding="utf-8",
    )
    return layout


def test_dag_discovers_ready_maintenance_checkpoint(tmp_path):
    layout = _layout(tmp_path)
    checkpoint_dir = layout.workspace_dir / "curation_records" / "evidence_checkpoints" / "maintenance-edge-0001"
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "curation_ready.json").write_text(json.dumps({
        "checkpoint_id": "maintenance-edge-0001",
        "status": "ready",
        "trigger_reason": "evidenced_parent_edge_correction",
    }), encoding="utf-8")
    (checkpoint_dir / "curator_brief.md").write_text("# Maintenance\n", encoding="utf-8")

    dag = DifficultyDagStore(layout.workspace_dir)

    assert "maintenance-edge-0001" in dag.pending_checkpoint_ids()
    assert dag.checkpoint_task_kind("maintenance-edge-0001") == "evidence_checkpoint"
    assert dag.checkpoint_artifact_path("maintenance-edge-0001") == checkpoint_dir / "curator_brief.md"


def test_curator_curation_archives_attempts_and_verified_outputs(tmp_path):
    layout = _layout(tmp_path)
    verified = layout.verified_dir / "bridge.md"
    verified.write_text("# Bridge\n", encoding="utf-8")
    layout.progress_audit_outcomes_path.write_text(json.dumps({
        "sequence": 1,
        "recorded_at": "2026-08-12T00:00:00Z",
        "worker_id": "worker-1",
        "difficulty_id": "bridge",
        "method_id": "construction",
        "status": "verified",
        "verified_file": str(verified),
    }) + "\n", encoding="utf-8")

    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "bridge",
            "statement": "Prove the bridge estimate.",
            "source_difficulty_ids": ["worker-bridge"],
            "status": "advanced",
        }],
    )

    node = dag.load()["nodes"]["bridge"]
    assert node["status"] == "advanced"
    assert node["progress"]["attempt_count"] == 1
    assert node["progress"]["verified_proposition_refs"] == ["verified_propositions/bridge.md"]


def test_curator_can_mark_an_existing_node_refuted_from_verified_evidence(tmp_path):
    layout = _layout(tmp_path)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "false-lower-bound",
            "statement": "Prove the proposed lower bound.",
            "source_handoff_ids": ["handoff-worker-a"],
        }],
    )

    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[],
        status_updates=[{
            "difficulty_id": "false-lower-bound",
            "status": "refuted",
            "evidence_refs": ["verified_propositions/counterexample.md"],
        }],
    )

    node = dag.load()["nodes"]["false-lower-bound"]
    assert node["status"] == "refuted"
    assert "verified_propositions/counterexample.md" in node["evidence_refs"]


def test_curator_rejects_open_prerequisite_under_resolved_parent(tmp_path):
    layout = _layout(tmp_path)
    dag = DifficultyDagStore(layout.workspace_dir)

    try:
        dag.record_curation(
            checkpoint_id="checkpoint-0001",
            difficulties=[
                {
                    "difficulty_id": "resolved-reduction",
                    "statement": "Complete the reduction.",
                    "source_difficulty_ids": ["source-reduction"],
                    "status": "resolved",
                },
                {
                    "difficulty_id": "open-bridge",
                    "statement": "Prove the bridge left by the reduction.",
                    "parent_difficulty_ids": ["resolved-reduction"],
                    "source_difficulty_ids": ["source-bridge"],
                    "status": "open",
                },
            ],
        )
    except ValueError as exc:
        assert "open prerequisite" in str(exc)
    else:
        raise AssertionError("an open prerequisite must not remain beneath a resolved parent")


def test_curator_can_remove_an_evidenced_wrong_parent_edge(tmp_path):
    layout = _layout(tmp_path)
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[
            {"difficulty_id": "parent", "statement": "Parent claim.", "source_difficulty_ids": ["source-parent"]},
            {"difficulty_id": "child", "statement": "Independent claim.", "parent_difficulty_ids": ["parent"], "source_difficulty_ids": ["source-child"]},
        ],
    )
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[],
        graph_corrections=[{
            "kind": "remove_parent_edge",
            "difficulty_id": "child",
            "parent_difficulty_id": "parent",
            "evidence_refs": ["verified_propositions/counterexample.md"],
        }],
    )
    assert dag.load()["nodes"]["child"]["parent_ids"] == []


def test_attempts_do_not_block_leaf_dispatch(tmp_path):
    layout = _layout(tmp_path)
    layout.progress_audit_outcomes_path.write_text("\n".join(
        json.dumps({
            "sequence": index,
            "recorded_at": f"2026-08-12T00:00:0{index}Z",
            "worker_id": f"worker-{index}",
            "difficulty_id": "open-leaf",
            "method_id": "direct_proof",
            "status": "rejected",
        })
        for index in range(1, 4)
    ) + "\n", encoding="utf-8")
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "open-leaf",
            "statement": "Prove the remaining estimate.",
            "source_difficulty_ids": ["worker-open-leaf"],
        }],
    )

    preflight = dag.dispatch_preflight(
        difficulty_id="open-leaf", method_id="direct_proof"
    )
    assert preflight["allowed"] is True
    assert dag.load()["nodes"]["open-leaf"]["progress"]["attempt_count"] == 3
