from __future__ import annotations

from pathlib import Path

import alphasolve
from alphasolve.agent import load_agent_suite
from alphasolve.llm.types import CompletionResponse, Message
from alphasolve.solver.orchestrator_agent_app import OrchestratorAgentApp


PACKAGE_ROOT = Path(alphasolve.__file__).resolve().parent


class _RecordingClient:
    def __init__(self, seen: dict[str, object]) -> None:
        self.seen = seen

    def complete(self, *, messages, tools, delta_sink=None):
        self.seen["messages"] = list(messages)
        self.seen["tools"] = list(tools)
        return CompletionResponse(
            message=Message(role="assistant", content="orchestrator profile ok"),
            finish_reason="stop",
        )


def test_orchestrator_agent_profile_uses_real_orchestrator_prompt_and_tools(tmp_path: Path):
    (tmp_path / "problem.md").write_text("# Problem\n\nFind the next proposition.\n", encoding="utf-8")
    (tmp_path / "verified_propositions").mkdir()
    for index in range(3):
        (tmp_path / "verified_propositions" / f"p{index + 1}.md").write_text(
            "# Proposition\n\n## Statement\nKnown fact.\n",
            encoding="utf-8",
        )
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge" / "notes.md").write_text("Useful context.\n", encoding="utf-8")
    assert not (tmp_path / "unverified_propositions").exists()

    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    seen: dict[str, object] = {}
    app = OrchestratorAgentApp(
        project_dir=tmp_path,
        suite=suite,
        client_factory=lambda config: _RecordingClient(seen),  # type: ignore[arg-type]
        max_workers=2,
        max_verify_rounds=1,
        verifier_scaling_factor=1,
        subagent_max_depth=0,
    )
    try:
        result = app.run_once("评估当前研究进展，暂时不要启动 worker。", event_sink=None)
    finally:
        app.close()

    assert result.final_answer == "orchestrator profile ok"
    messages = seen["messages"]
    assert isinstance(messages, list)
    assert messages[0].role == "system"
    assert messages[0].content == suite.agents["orchestrator"].system_prompt
    assert messages[-1].role == "user"
    assert messages[-1].content == "评估当前研究进展，暂时不要启动 worker。"
    tool_names = [tool.name for tool in seen["tools"]]
    assert tool_names == list(suite.agents["orchestrator"].tools)
    assert "SpawnWorker" in tool_names
    assert "TaskOutput" in tool_names
    assert "Agent" in tool_names
    assert not (tmp_path / "unverified_propositions").exists()


def test_orchestrator_agent_snapshot_layout_treats_cwd_as_workspace(tmp_path: Path):
    suite = load_agent_suite(PACKAGE_ROOT / "solver" / "config")
    app = OrchestratorAgentApp(
        project_dir=tmp_path,
        suite=suite,
        client_factory=lambda config: _RecordingClient({}),  # type: ignore[arg-type]
    )
    try:
        assert app.layout.project_root == tmp_path.resolve()
        assert app.layout.workspace_dir == tmp_path.resolve()
        assert app.layout.verified_dir == tmp_path.resolve() / "verified_propositions"
        assert app.layout.unverified_dir == tmp_path.resolve() / "unverified_propositions"
    finally:
        app.close()
