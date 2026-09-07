"""generator 工具测试入口与正式角色共用权限和工具覆盖。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import alphasolve
from alphasolve.agent import load_agent_suite
from alphasolve.solver import role
from alphasolve.solver.role_agent_app import GeneratorAgentApp


PACKAGE_ROOT = Path(alphasolve.__file__).resolve().parent


def test_generator_profile_preserves_production_tools_and_worker_isolation(tmp_path, monkeypatch):
    built = []

    def fake_agent(**kwargs):
        agent = SimpleNamespace(**kwargs, close=lambda: None)
        built.append(agent)
        return agent

    monkeypatch.setattr(role, "Agent", fake_agent)
    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    own_dir = tmp_path / "unverified_propositions" / "worker_1"
    other_dir = tmp_path / "unverified_propositions" / "worker_2"
    own_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (own_dir / "note.md").write_text("own evidence", encoding="utf-8")
    (other_dir / "note.md").write_text("other worker draft", encoding="utf-8")
    app = GeneratorAgentApp(
        project_dir=tmp_path,
        suite=suite,
        worker_dir="unverified_propositions/worker_1",
        client_factory=lambda config: object(),
    )
    try:
        definitions = app.tool_defs()
        agent = app.build_agent()
        assert len(built) == 1
        assert agent.config is suite.agents["generator"]
        assert [tool.name for tool in definitions] == list(agent.config.tools)
        read_def = next(tool for tool in definitions if tool.name == "Read")
        assert "workspace access restrictions" in read_def.description
        agent_def = next(tool for tool in definitions if tool.name == "Agent")
        assert agent_def.parameters["properties"]["type"]["enum"] == [
            "compute_subagent", "numerical_experiment_subagent", "reasoning_subagent",
        ]
        own_result = agent.tool_registry.execute("Read", {"path": "unverified_propositions/worker_1/note.md"})
        other_result = agent.tool_registry.execute("Read", {"path": "unverified_propositions/worker_2/note.md"})
        assert not own_result.is_error
        assert "own evidence" in own_result.content
        assert other_result.is_error
        assert "other worker draft" not in other_result.content
    finally:
        app.close()


def test_generator_profile_rejects_worker_directory_outside_unverified_area(tmp_path):
    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    with pytest.raises(ValueError, match="unverified_propositions"):
        GeneratorAgentApp(
            project_dir=tmp_path,
            suite=suite,
            worker_dir="verified_propositions",
            client_factory=lambda config: object(),
        )
