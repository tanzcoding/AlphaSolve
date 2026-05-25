from __future__ import annotations

from typing import Any

from ..config import AgentConfig
from .registry import ToolRegistry
from .types import SubagentDispatcher, ToolResult


def register_agent_tool(
    registry: ToolRegistry,
    *,
    agent_config: AgentConfig,
    dispatcher: SubagentDispatcher,
    depth: int = 0,
) -> None:
    """Register the Agent tool with a description and enum scoped to this agent's permissions.

    第三层（或其他实现）通过提供 SubagentDispatcher 实例注入业务 subagent 类型清单。
    """
    if "Agent" not in agent_config.tools:
        return
    agent_tool_params = agent_config.tool_parameters.get("Agent", {})
    type_constraint = agent_tool_params.get("type", {})
    allowed_types = type_constraint.get("enum", dispatcher.available_types())
    known = set(dispatcher.available_types())
    allowed_types = [t for t in allowed_types if t in known]
    if not allowed_types:
        return
    lines = [
        "Launch a new specialized agent and return its final report.",
        "",
        "The launched agent runs in a separate session. It does not see this conversation unless you include the needed context in `prompt`.",
        "",
        "Available agent types:",
    ]
    for stype in allowed_types:
        when = dispatcher.describe_type(stype)
        lines.append(f"- {stype}: {when}")
    lines.extend([
        "",
        "## Writing the prompt",
        "",
        "- For broad workspace review, split by directory or file group: ask each subagent to inspect one scoped area and return a concise evidence report with file paths, line references when available, current status, open issues, and recommended next local step.",
        "- Use the main agent to compare subagent reports and decide the global next step; do not ask a subagent with a narrow scope to make the final workspace-wide decision.",
        "- State the exact claim, definitions, assumptions, and what needs to be proved or checked.",
        "- Include relevant context: what's already known, what approaches have been tried, which files to reference, and why this task matters.",
        "- Give enough context that the agent can make judgment calls rather than just following a narrow instruction.",
        "- Keep the task self-contained and limited to the agent's scope.",
    ])

    def _handler(args: dict[str, Any]) -> ToolResult:
        agent_type = str(args.get("type") or "")
        description = str(args.get("description") or "")
        prompt = str(args.get("prompt") or "")
        if not prompt.strip():
            return ToolResult("ERROR: prompt must not be empty", is_error=True)
        try:
            text = dispatcher.call(agent_type, description, prompt, depth=depth)
            return ToolResult(text)
        except Exception as exc:
            return ToolResult(f"ERROR: {exc}", is_error=True)

    registry.register(
        name="Agent",
        description="\n".join(lines),
        parameters={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": allowed_types, "description": "The type of specialized agent to use for this task."},
                "description": {"type": "string", "description": "A short (3-5 word) description of the task."},
                "prompt": {"type": "string", "description": "The task for the agent to perform."},
            },
            "required": ["type", "description", "prompt"],
        },
        handler=_handler,
    )
