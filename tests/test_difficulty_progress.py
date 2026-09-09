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


def test_reviewer_projection_precomputes_attempt_statistics(tmp_path):
    """method_attempt_counts / consecutive_no_progress / underexplored_pairs 必须由运行时算好。

    这些是事实计算而非策略：reviewer 不应从 attempt 列表里自己数，代码也不替它排序选路。
    """
    from alphasolve.solver.difficulty_dag import DifficultyDagStore

    workspace = tmp_path / "workspace"
    (workspace / "curation_records").mkdir(parents=True)
    dag = DifficultyDagStore(workspace)

    checkpoint = workspace / "progress_audits" / "checkpoint-0001"
    checkpoint.mkdir(parents=True)
    (checkpoint / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed"}), encoding="utf-8"
    )
    (workspace / "progress_audit_outcomes.jsonl").write_text(
        "\n".join(json.dumps(item) for item in [
            {"sequence": 1, "recorded_at": "2026-01-01T00:00:00Z", "difficulty_id": "leaf",
             "method_id": "direct_proof", "status": "rejected", "verified_file": ""},
            {"sequence": 2, "recorded_at": "2026-01-02T00:00:00Z", "difficulty_id": "leaf",
             "method_id": "direct_proof", "status": "verified",
             "verified_file": str(workspace / "verified_propositions" / "a.md")},
            {"sequence": 3, "recorded_at": "2026-01-03T00:00:00Z", "difficulty_id": "leaf",
             "method_id": "contradiction", "status": "rejected", "verified_file": ""},
            {"sequence": 4, "recorded_at": "2026-01-04T00:00:00Z", "difficulty_id": "leaf",
             "method_id": "contradiction", "status": "rejected", "verified_file": ""},
        ]) + "\n",
        encoding="utf-8",
    )

    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "leaf",
            "statement": "Bound the packing term.",
            "source_handoff_ids": ["handoff-a"],
        }],
    )

    projection = dag.reviewer_graph_projection()
    node = next(item for item in projection["nodes"] if item["difficulty_id"] == "leaf")
    progress = node["progress"]

    assert progress["method_attempt_counts"] == {"contradiction": 2, "direct_proof": 2}
    # 最近两次 contradiction 均无 verified 产出 → 连续无进展为 2。
    assert progress["consecutive_no_progress"] == 2
    assert progress["last_verified_at"] == "2026-01-02T00:00:00Z"
    assert progress["last_delivered_at"] == "2026-01-02T00:00:00Z"
    assert progress["method_outcome_breakdown"]["direct_proof"] == {
        "attempts": 2,
        "verified_yields": 1,
        "delivered_yields": 1,
        "off_target_verified": 0,
        "barren": 1,
    }
    assert "construction" in progress["untried_methods"]
    assert "direct_proof" not in progress["untried_methods"]

    pair = next(item for item in projection["underexplored_pairs"] if item["difficulty_id"] == "leaf")
    assert pair["consecutive_no_progress"] == 2
    assert "computation" in pair["untried_methods"]


def test_terminal_node_is_excluded_from_underexplored_pairs(tmp_path):
    from alphasolve.solver.difficulty_dag import DifficultyDagStore

    workspace = tmp_path / "workspace"
    (workspace / "curation_records").mkdir(parents=True)
    dag = DifficultyDagStore(workspace)
    checkpoint = workspace / "progress_audits" / "checkpoint-0001"
    checkpoint.mkdir(parents=True)
    (checkpoint / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed"}), encoding="utf-8"
    )
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "settled",
            "statement": "Already settled obligation.",
            "source_handoff_ids": ["handoff-settled"],
            "status": "resolved",
        }],
    )

    projection = dag.reviewer_graph_projection()

    assert [item["difficulty_id"] for item in projection["underexplored_pairs"]] == []


def test_off_target_verified_output_is_not_a_delivered_route_yield(tmp_path):
    from alphasolve.solver.difficulty_dag import DifficultyDagStore

    workspace = tmp_path / "workspace"
    (workspace / "curation_records").mkdir(parents=True)
    checkpoint = workspace / "progress_audits" / "checkpoint-0001"
    checkpoint.mkdir(parents=True)
    (checkpoint / "decision.json").write_text(
        json.dumps({"checkpoint_id": "checkpoint-0001", "status": "completed"}), encoding="utf-8"
    )
    verified = workspace / "verified_propositions" / "side-result.md"
    verified.parent.mkdir(parents=True)
    verified.write_text("# Side result\n", encoding="utf-8")
    (workspace / "progress_audit_outcomes.jsonl").write_text(json.dumps({
        "sequence": 1,
        "recorded_at": "2026-01-01T00:00:00Z",
        "worker_id": "worker-side",
        "difficulty_id": "leaf",
        "method_id": "direct_proof",
        "route_label": "main-route",
        "status": "verified",
        "delivery": "off_target",
        "verified_file": str(verified),
    }) + "\n", encoding="utf-8")

    dag = DifficultyDagStore(workspace)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "leaf",
            "statement": "Prove the assigned target.",
            "source_handoff_ids": ["handoff-side"],
        }],
    )

    node = next(item for item in dag.reviewer_graph_projection()["nodes"] if item["difficulty_id"] == "leaf")
    progress = node["progress"]
    assert progress["consecutive_no_progress"] == 1
    assert progress["last_delivered_at"] == ""
    assert progress["method_outcome_breakdown"]["direct_proof"] == {
        "attempts": 1,
        "verified_yields": 1,
        "delivered_yields": 0,
        "off_target_verified": 1,
        "barren": 0,
    }


def test_canonical_node_keeps_full_attempt_history_for_long_term_strategy(tmp_path):
    layout = _layout(tmp_path)
    outcomes = [
        {
            "sequence": index,
            "recorded_at": f"2026-01-{(index % 28) + 1:02d}T00:00:00Z",
            "worker_id": f"worker-{index}",
            "difficulty_id": "long-lived",
            "method_id": "direct_proof",
            "route_label": "shared-route",
            "status": "rejected",
            "failure_kind": "verification_rejected",
        }
        for index in range(1, 82)
    ]
    layout.progress_audit_outcomes_path.write_text(
        "\n".join(json.dumps(item) for item in outcomes) + "\n",
        encoding="utf-8",
    )
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{
            "difficulty_id": "long-lived",
            "statement": "Prove the long-lived target.",
            "source_handoff_ids": ["handoff-long-lived"],
        }],
    )

    stored = dag.load()["nodes"]["long-lived"]["progress"]
    projection = next(
        item for item in dag.reviewer_graph_projection()["nodes"]
        if item["difficulty_id"] == "long-lived"
    )

    assert stored["attempt_count"] == 81
    assert len(stored["attempts"]) == 81
    assert projection["progress"]["method_attempt_counts"]["direct_proof"] == 81


def test_shared_obstacle_clusters_group_distinct_nodes_with_identical_obstacle_text(tmp_path):
    """No curator/reviewer LLM judgment involved: this is a pure text-match runtime fact.

    Two different canonical nodes whose worker-reported obstacles happen to read
    identically are flagged so a reviewer or curator can decide whether it is a true
    duplicate, a shared root-architecture assumption, or coincidental wording -- see
    `_shared_obstacle_clusters`.
    """
    layout = _layout(tmp_path)
    shared_obstacle = "Independent local contributions do not compose into a global soundness bound."
    outcomes = [
        {
            "sequence": 1,
            "recorded_at": "2026-01-01T00:00:00Z",
            "worker_id": "worker-1",
            "difficulty_id": "leaf-a",
            "method_id": "direct_proof",
            "status": "rejected",
            "difficulty_handoff": {"obstacle": shared_obstacle},
        },
        {
            "sequence": 2,
            "recorded_at": "2026-01-02T00:00:00Z",
            "worker_id": "worker-2",
            "difficulty_id": "leaf-b",
            "method_id": "construction",
            "status": "rejected",
            "difficulty_handoff": {"obstacle": shared_obstacle},
        },
        {
            "sequence": 3,
            "recorded_at": "2026-01-03T00:00:00Z",
            "worker_id": "worker-3",
            "difficulty_id": "leaf-c",
            "method_id": "contradiction",
            "status": "rejected",
            "difficulty_handoff": {"obstacle": "A completely unrelated short-form obstacle text."},
        },
    ]
    layout.progress_audit_outcomes_path.write_text(
        "\n".join(json.dumps(item) for item in outcomes) + "\n",
        encoding="utf-8",
    )
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[
            {"difficulty_id": "leaf-a", "statement": "Prove leaf A.", "source_handoff_ids": ["handoff-a"]},
            {"difficulty_id": "leaf-b", "statement": "Prove leaf B.", "source_handoff_ids": ["handoff-b"]},
            {"difficulty_id": "leaf-c", "statement": "Prove leaf C.", "source_handoff_ids": ["handoff-c"]},
        ],
    )

    projection = dag.reviewer_graph_projection()
    clusters = projection["shared_obstacle_clusters"]

    assert len(clusters) == 1
    assert clusters[0]["difficulty_ids"] == ["leaf-a", "leaf-b"]
    assert clusters[0]["obstacle_digest"] == shared_obstacle.lower()


def test_shared_obstacle_clusters_ignore_short_digests_and_single_node_matches(tmp_path):
    layout = _layout(tmp_path)
    outcomes = [
        {
            "sequence": 1,
            "recorded_at": "2026-01-01T00:00:00Z",
            "worker_id": "worker-1",
            "difficulty_id": "leaf-a",
            "method_id": "direct_proof",
            "status": "rejected",
            # Too short to be meaningful evidence of a genuinely shared obstacle.
            "difficulty_handoff": {"obstacle": "Gap."},
        },
        {
            "sequence": 2,
            "recorded_at": "2026-01-02T00:00:00Z",
            "worker_id": "worker-2",
            "difficulty_id": "leaf-a",
            "method_id": "construction",
            "status": "rejected",
            # Same node repeating its own obstacle must not count as a cross-node cluster.
            "difficulty_handoff": {"obstacle": "The exact same long obstacle text repeated twice here."},
        },
        {
            "sequence": 3,
            "recorded_at": "2026-01-03T00:00:00Z",
            "worker_id": "worker-3",
            "difficulty_id": "leaf-a",
            "method_id": "contradiction",
            "status": "rejected",
            "difficulty_handoff": {"obstacle": "The exact same long obstacle text repeated twice here."},
        },
    ]
    layout.progress_audit_outcomes_path.write_text(
        "\n".join(json.dumps(item) for item in outcomes) + "\n",
        encoding="utf-8",
    )
    dag = DifficultyDagStore(layout.workspace_dir)
    dag.record_curation(
        checkpoint_id="checkpoint-0001",
        difficulties=[{"difficulty_id": "leaf-a", "statement": "Prove leaf A.", "source_handoff_ids": ["handoff-a"]}],
    )

    projection = dag.reviewer_graph_projection()
    assert projection["shared_obstacle_clusters"] == []
