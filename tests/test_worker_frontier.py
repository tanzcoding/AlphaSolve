from alphasolve.solver.project import ProjectLayout
from alphasolve.solver.worker import Worker


def _worker(tmp_path, hint: str = "graph analysis proof") -> Worker:
    (tmp_path / "problem.md").write_text("# Problem\n\nTest.\n", encoding="utf-8")
    layout = ProjectLayout.create(tmp_path)
    layout.ensure()
    return Worker(
        layout=layout,
        suite=object(),
        client_factory=lambda _config: None,
        worker_hint=hint,
        frontier_note="Test frontier.",
    )


def test_knowledge_scaffold_uses_canonical_paths_and_explicit_status(tmp_path):
    worker = _worker(tmp_path)
    knowledge_dir = worker.layout.knowledge_dir

    (knowledge_dir / "useful.md").write_text(
        "---\nmodification_count: 1\n---\n\n# Useful Graph Proof\n\n"
        "This graph analysis proof is reusable.\n",
        encoding="utf-8",
    )
    (knowledge_dir / "historical.md").write_text(
        "# Historical Note\n\nA graph claim was refuted in an unrelated setting.\n",
        encoding="utf-8",
    )
    for index in range(6):
        (knowledge_dir / f"invalidated-{index}.md").write_text(
            "---\nknowledge_status: invalidated\n---\n\n"
            f"# Invalidated Route {index}\n\n"
            "This graph analysis proof route is invalidated.\n",
            encoding="utf-8",
        )

    frontier = worker._scan_knowledge_scaffold()

    assert "knowledge/useful.md" in frontier
    assert "Useful Graph Proof" in frontier
    assert "knowledge/knowledge/" not in frontier
    assert "historical.md" not in frontier
    assert frontier.count("knowledge/invalidated-") == 5
    assert "Invalidated Route 0" in frontier


def test_generator_task_uses_provided_frontier_snapshot(tmp_path):
    worker = _worker(tmp_path)

    task = worker._generator_task("fixed frontier snapshot")

    assert "# Curated Frontier\n\nfixed frontier snapshot" in task
