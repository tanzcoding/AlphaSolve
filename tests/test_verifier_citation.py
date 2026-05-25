import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import alphasolve  # noqa: E402
from alphasolve.agent import Workspace, load_agent_suite  # noqa: E402
from alphasolve.solver.project import ProjectLayout  # noqa: E402
from alphasolve.agent.tools import build_default_tool_registry  # noqa: E402
from alphasolve.solver.workspace_access import RoleWorkspaceAccess  # noqa: E402
from alphasolve.solver.worker import Worker  # noqa: E402
PACKAGE_ROOT = pathlib.Path(alphasolve.__file__).resolve().parent


def test_verifier_format_references_is_first_default_attempt():
    suite = load_agent_suite(pathlib.Path(PACKAGE_ROOT) / "solver" / "config" / "agents.yaml")

    assert "verifier_format_references" in suite.agents
    assert "verifier_citation" in suite.agents
    assert suite.settings["verifier_agents"][0] == "verifier_format_references"
    assert suite.settings["verifier_agents"][1] == "verifier_citation"
    assert suite.settings["verifier_scaling_factor"] == 5
    assert "knowledge/references/" in suite.agents["verifier_format_references"].system_prompt
    assert "exactly two Markdown sections" in suite.agents["verifier_format_references"].system_prompt
    assert "pure mathematical statement" in suite.agents["verifier_format_references"].system_prompt
    assert "knowledge/" in suite.agents["verifier_citation"].system_prompt
    assert "path relative to `verified_propositions`" in suite.agents["verifier_citation"].system_prompt
    assert r"\ref{coercive\energy-estimate}" in suite.agents["verifier_citation"].system_prompt
    assert "Agent" in suite.agents["verifier_citation"].tools


def test_verifier_task_runs_format_reference_gate_before_citation_audit(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "problem.md").write_text("# Problem\n\nProve a lemma.\n", encoding="utf-8")
    layout = ProjectLayout.create(project_dir)
    layout.ensure()
    worker = Worker(
        layout=layout,
        suite=SimpleNamespace(agents={}, settings={}),
        client_factory=lambda config: None,
        verifier_scaling_factor=5,
    )
    worker.worker_dir.mkdir(parents=True, exist_ok=True)
    proposition_file = worker.worker_dir / "proposition.md"
    proposition_file.write_text("# Proposition\n", encoding="utf-8")

    format_task = worker._verifier_task(
        proposition_file,
        workflow_index=1,
        attempt_index=1,
        attempt_total=5,
        config_name="verifier_format_references",
    )
    citation_task = worker._verifier_task(
        proposition_file,
        workflow_index=1,
        attempt_index=2,
        attempt_total=5,
        config_name="verifier_citation",
    )
    math_task = worker._verifier_task(
        proposition_file,
        workflow_index=1,
        attempt_index=3,
        attempt_total=5,
        config_name="verifier_stepwise",
    )

    assert "first format/reference-source gate" in format_task
    assert "exactly two Markdown sections" in format_task
    assert "knowledge/references/" in format_task
    assert "lemma/proposition/theorem/claim-labeled" in format_task
    assert "citation/reference audit" in citation_task
    assert "path relative to `verified_propositions`" in citation_task
    assert r"\ref{category\filename}" in citation_task
    assert "must not cite, depend on, or present as established any proposition from `knowledge/`" in citation_task
    assert "reasoning_subagent" in citation_task
    assert "conditions" in citation_task
    assert "Earlier verifier attempts audit file format" in math_task
    assert "citation/reference audit" not in math_task


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
        verifier_scaling_factor=5,
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
    suite = load_agent_suite(pathlib.Path(PACKAGE_ROOT) / "solver" / "config" / "agents.yaml")
    config = suite.agents["verifier_citation"]
    workspace = Workspace(tmp_path)
    nested_dir = tmp_path / "verified_propositions" / "coercive" / "local"
    nested_dir.mkdir(parents=True)
    (nested_dir / "energy.md").write_text("# Energy\n", encoding="utf-8")

    registry = build_default_tool_registry(RoleWorkspaceAccess(workspace=workspace))

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


def test_grep_defaults_to_regex_matching(tmp_path):
    workspace = Workspace(tmp_path)
    target = tmp_path / "verified_propositions" / "coercive"
    target.mkdir(parents=True)
    (target / "energy.md").write_text("# Energy\n", encoding="utf-8")

    registry = build_default_tool_registry(RoleWorkspaceAccess(workspace=workspace))
    result = registry.execute(
        "Grep",
        {"path": "verified_propositions", "pattern": r"^# Energy$"},
    )

    assert not result.is_error
    assert "verified_propositions/coercive/energy.md" in result.content
