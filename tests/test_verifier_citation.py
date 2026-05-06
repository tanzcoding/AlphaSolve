import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agents.general import Workspace, load_agent_suite_config  # noqa: E402
from alphasolve.agents.team.project import ProjectLayout  # noqa: E402
from alphasolve.agents.team.tools import RoleWorkspaceAccess, build_workspace_tool_registry  # noqa: E402
from alphasolve.agents.team.worker import Worker  # noqa: E402
from alphasolve.config.agent_config import PACKAGE_ROOT  # noqa: E402


def test_verifier_citation_is_first_default_attempt():
    suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config" / "agents.yaml")

    assert "verifier_citation" in suite.agents
    assert suite.settings["verifier_agents"][0] == "verifier_citation"
    assert suite.settings["verifier_scaling_factor"] == 4
    assert "knowledge/" in suite.agents["verifier_citation"].system_prompt
    assert "path relative to `verified_propositions`" in suite.agents["verifier_citation"].system_prompt
    assert r"\ref{coercive\energy-estimate}" in suite.agents["verifier_citation"].system_prompt
    assert "Agent" not in suite.agents["verifier_citation"].tools


def test_verifier_task_offloads_citation_audit_to_first_attempt(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "problem.md").write_text("# Problem\n\nProve a lemma.\n", encoding="utf-8")
    layout = ProjectLayout.create(project_dir)
    layout.ensure()
    worker = Worker(
        layout=layout,
        suite=SimpleNamespace(agents={}, settings={}),
        client_factory=lambda config: None,
        verifier_scaling_factor=4,
    )
    worker.worker_dir.mkdir(parents=True, exist_ok=True)
    proposition_file = worker.worker_dir / "proposition.md"
    proposition_file.write_text("# Proposition\n", encoding="utf-8")

    citation_task = worker._verifier_task(
        proposition_file,
        workflow_index=1,
        attempt_index=1,
        attempt_total=4,
        config_name="verifier_citation",
    )
    math_task = worker._verifier_task(
        proposition_file,
        workflow_index=1,
        attempt_index=2,
        attempt_total=4,
        config_name="verifier_stepwise",
    )

    assert "perform only the citation/reference audit" in citation_task
    assert "path relative to `verified_propositions`" in citation_task
    assert r"\ref{category\filename}" in citation_task
    assert "must not cite, depend on, or present as established any proposition from `knowledge/`" in citation_task
    assert "A separate first verifier attempt audits" in math_task
    assert "perform only the citation/reference audit" not in math_task


def test_worker_tasks_describe_full_verified_proposition_reference_paths(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "problem.md").write_text("# Problem\n\nProve a lemma.\n", encoding="utf-8")
    layout = ProjectLayout.create(project_dir)
    layout.ensure()
    worker = Worker(
        layout=layout,
        suite=SimpleNamespace(agents={}, settings={}),
        client_factory=lambda config: None,
        verifier_scaling_factor=4,
    )
    worker.worker_dir.mkdir(parents=True, exist_ok=True)
    verified_file = layout.verified_dir / "category" / "proposition.md"
    verified_file.parent.mkdir(parents=True, exist_ok=True)
    verified_file.write_text("# Proposition\n", encoding="utf-8")

    generator_task = worker._generator_task()
    theorem_task = worker._theorem_checker_task(verified_file, attempt_index=1)

    assert r"\ref{filename-without-extension}" not in generator_task
    assert "path is relative to `verified_propositions`" in generator_task
    assert r"\ref{category\filename}" in generator_task
    assert r"\ref{filename-without-extension}" not in theorem_task
    assert "path is relative to `verified_propositions`" in theorem_task
    assert r"\ref{category\filename}" in theorem_task


def test_citation_access_denies_knowledge_reads(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "summary.md").write_text("planning summary", encoding="utf-8")
    (tmp_path / "verified_propositions").mkdir()
    (tmp_path / "verified_propositions" / "lemma.md").write_text("verified", encoding="utf-8")

    access = RoleWorkspaceAccess(workspace=workspace, deny_read_rels=("knowledge",))

    assert "knowledge/" not in access.list_dir(".")
    assert "verified" in access.read_text_page("verified_propositions/lemma.md").output
    with pytest.raises(ValueError, match="knowledge"):
        access.read_text_page("knowledge/summary.md")


def test_citation_tools_can_list_verified_proposition_subdirectories(tmp_path):
    suite = load_agent_suite_config(pathlib.Path(PACKAGE_ROOT) / "config" / "agents.yaml")
    config = suite.agents["verifier_citation"]
    workspace = Workspace(tmp_path)
    nested_dir = tmp_path / "verified_propositions" / "coercive" / "local"
    nested_dir.mkdir(parents=True)
    (nested_dir / "energy.md").write_text("# Energy\n", encoding="utf-8")

    registry = build_workspace_tool_registry(RoleWorkspaceAccess(workspace=workspace))

    listed = registry.execute(
        "ListDir",
        {"path": "verified_propositions/coercive/local"},
        enabled=config.tools,
        tool_parameters=config.tool_parameters,
    )
    globbed = registry.execute(
        "Glob",
        {"path": "verified_propositions/coercive/local", "pattern": "*.md"},
        enabled=config.tools,
        tool_parameters=config.tool_parameters,
    )

    assert not listed.is_error
    assert "energy.md" in listed.content
    assert not globbed.is_error
    assert "verified_propositions/coercive/local/energy.md" in globbed.content
