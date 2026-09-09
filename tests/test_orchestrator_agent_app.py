"""真实 orchestrator 的工具测试入口不重放可见历史，也不在展示工具时启动任务。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import alphasolve
import pytest
from alphasolve.agent import AgentRunError, load_agent_suite
from alphasolve.solver import orchestrator
from alphasolve.solver.orchestrator_agent_app import OrchestratorAgentApp


PACKAGE_ROOT = Path(alphasolve.__file__).resolve().parent


class _FakeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.calls = []
        self.closed = False

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return SimpleNamespace(final_answer="orchestrator profile ok", messages=[], trace=[])

    def close(self):
        self.closed = True


def test_orchestrator_profile_uses_production_prompt_tools_and_same_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "Agent", _FakeAgent)
    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    app = OrchestratorAgentApp(project_dir=tmp_path, suite=suite, client_factory=lambda config: object())
    prepared = []
    monkeypatch.setattr(app._cold_start_runtime_or_create(), "prepare", lambda manager: prepared.append(manager))
    try:
        definitions = app.tool_defs()
        agent = app.build_agent()
        assert not prepared
        assert not agent.calls
        assert agent.config.system_prompt == suite.agents["orchestrator"].system_prompt
        assert [tool.name for tool in definitions] == list(suite.agents["orchestrator"].tools)
        assert "SpawnWorker" in agent.config.tools
        assert "TaskOutput" in agent.config.tools
        assert "RequestResearchPlan" in agent.config.tools
        assert "Agent" not in agent.config.tools
        app.run_once("first", event_sink=None)
        app.run_once("second", event_sink=None)
        assert not prepared
        assert app.build_agent() is agent
        assert [task for task, kwargs in agent.calls] == ["first", "second"]
        assert all("extra_messages" not in kwargs for task, kwargs in agent.calls)
        assert not (tmp_path / "unverified_propositions").exists()
    finally:
        app.close()
    assert agent.closed


def test_orchestrator_snapshot_layout_treats_cwd_as_workspace(tmp_path):
    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    app = OrchestratorAgentApp(project_dir=tmp_path, suite=suite, client_factory=lambda config: object())
    try:
        assert app.layout.project_root == tmp_path.resolve()
        assert app.layout.workspace_dir == tmp_path.resolve()
        assert app.layout.verified_dir == tmp_path.resolve() / "verified_propositions"
    finally:
        app.close()


def test_orchestrator_quota_failure_stops_workers_before_returning_to_cli(tmp_path, monkeypatch):
    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    app = OrchestratorAgentApp(project_dir=tmp_path, suite=suite, client_factory=lambda config: object())

    def fail_run(*args, **kwargs):
        raise AgentRunError("subscription exhausted", trace=[], failure_kind="quota")

    monkeypatch.setattr(app, "build_agent", lambda: SimpleNamespace(run=fail_run))
    try:
        with pytest.raises(AgentRunError, match="subscription exhausted"):
            app.run_once("continue", event_sink=None)
        assert app.stop_event.is_set()
        assert app.worker_stop_event.is_set()
    finally:
        app.close()
