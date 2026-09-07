from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.agent import AgentConfig, Workspace  # noqa: E402
from alphasolve.llm.types import Message  # noqa: E402
from tests.response_fakes import CompletionResponse  # noqa: E402
from alphasolve.solver.subagent_service import SubagentService  # noqa: E402
from alphasolve.solver.tool_runtime import build_solver_tool_registry  # noqa: E402
from alphasolve.solver.workspace_access import RoleWorkspaceAccess  # noqa: E402


class _CapturingClient:
    def __init__(self, seen_tools: list[str]) -> None:
        self.seen_tools = seen_tools

    def complete(self, *, messages, tools, delta_sink=None):
        self.seen_tools.extend(tool.description for tool in tools)
        return CompletionResponse(
            message=Message(role="assistant", content="done"),
            finish_reason="stop",
        )


def test_build_solver_tool_registry_registers_third_layer_research_tools(tmp_path: Path):
    registry = build_solver_tool_registry(Workspace(tmp_path))
    names = {tool.name for tool in registry.registered_tools()}

    assert "ResearchProgressReview" in names
    assert "InspectMarkdown" in names


def test_subagent_runtime_tool_filter_preserves_tool_descriptions(tmp_path: Path):
    config = AgentConfig(
        name="researcher",
        system_prompt="You inspect files.",
        tools=("Read", "Agent"),
        tool_descriptions={"Read": {"suffix": "CUSTOM READ DESCRIPTION"}},
    )
    suite = SimpleNamespace(subagents={"researcher": config})
    seen_descriptions: list[str] = []
    service = SubagentService(
        suite=suite,
        client_factory=lambda _config: _CapturingClient(seen_descriptions),
        max_depth=0,
        file_access_factory=lambda: RoleWorkspaceAccess(workspace=Workspace(tmp_path)),
    )

    service.call("researcher", "check", "read something")

    assert any("CUSTOM READ DESCRIPTION" in description for description in seen_descriptions)
