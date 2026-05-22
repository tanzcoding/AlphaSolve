"""通用文件型 agent 基础设施。"""

from .config import AgentConfig, AgentSuite, load_agent_config, load_agent_suite
from .agent import Agent, AgentEventSink, AgentRunError, AgentRunResult
from .tools import SubagentDispatcher, ToolRegistry, ToolResult, build_default_tool_registry, register_agent_tool
from .workspace import Workspace, WorkspaceLike

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentEventSink",
    "AgentRunResult",
    "AgentRunError",
    "AgentSuite",
    "SubagentDispatcher",
    "ToolRegistry",
    "ToolResult",
    "Workspace",
    "WorkspaceLike",
    "build_default_tool_registry",
    "load_agent_config",
    "load_agent_suite",
    "register_agent_tool",
]
