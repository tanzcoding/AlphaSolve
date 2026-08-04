from __future__ import annotations

from typing import Any, Callable

from ..config import AgentConfig
from .registry import ToolRegistry
from .types import SubagentDispatcher, ToolResult


def register_agent_tool(
    registry: ToolRegistry,
    *,
    agent_config: AgentConfig,
    dispatcher: SubagentDispatcher,
    depth: int = 0,
    read_state_resolver: Callable[[str, Any], bool | None] | None = None,
) -> None:
    """Register the Agent tool with a description and enum scoped to this agent's permissions.

    第三层（或其他实现）通过提供 SubagentDispatcher 实例注入业务 subagent 类型清单。
    ``read_state_resolver`` 是可选策略钩子：给定 (agent_type, 调用方请求的 read_state)，
    返回最终生效的 read_state（``True``/``False`` 注入指令，``None`` 表示不注入）。第三层可
    借此对某类 subagent（如 research_reviewer）以 epsilon-greedy 等策略由系统决定是否读 state.md，
    而不完全交给调用方 LLM。
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
        # 可选的 read_state 开关：以结构化指令注入被调 agent 的 prompt，由其提示词决定语义。
        # 目前仅 research_reviewer 会据此决定是否读取 verified_propositions/**/state.md。
        # 调用方（LLM）可显式传 read_state；若第三层提供了 read_state_resolver，则由该策略
        # 钩子给出最终值（例如 orchestrator 对 research_reviewer 用 epsilon-greedy 由系统决定）。
        effective_read_state = args.get("read_state")
        if read_state_resolver is not None:
            effective_read_state = read_state_resolver(agent_type, effective_read_state)
        if effective_read_state is not None:
            if bool(effective_read_state):
                directive = (
                    "[read_state=true] You MAY consult verified_propositions/**/state.md (the orchestrator's "
                    "prior strategic notes) as a hypothesis to sanity-check, but treat it as possibly stale or mistaken."
                )
            else:
                directive = (
                    "[read_state=false] Do NOT read verified_propositions/**/state.md; form your assessment only "
                    "from the problem, the verified proposition proofs, index.md, and knowledge/."
                )
            prompt = f"{directive}\n\n{prompt}"
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
                "read_state": {
                    "type": "boolean",
                    "description": (
                        "Optional; only meaningful for research_reviewer. If true, the reviewer may consult "
                        "verified_propositions/**/state.md (prior strategic notes). If false or omitted, the reviewer "
                        "forms an independent assessment and does not read state.md, avoiding being steered by a previous wrong route."
                    ),
                },
            },
            "required": ["type", "description", "prompt"],
        },
        handler=_handler,
    )
