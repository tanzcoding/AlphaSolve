"""通用文件型 agent 基础设施。"""

from .config import AgentSuiteConfig, GeneralAgentConfig, load_agent_suite_config, load_general_agent_config
from .general_agent import AgentEventSink, AgentRunError, AgentRunResult, GeneralPurposeAgent
from .tool_registry import SubagentDispatcher, ToolRegistry, ToolResult, build_default_tool_registry, register_agent_tool
from .workspace import Workspace, WorkspaceLike

__all__ = [
    "AgentEventSink",
    "AgentRunResult",
    "AgentRunError",
    "AgentSuiteConfig",
    "GeneralAgentConfig",
    "GeneralPurposeAgent",
    "SubagentDispatcher",
    "ToolRegistry",
    "ToolResult",
    "Workspace",
    "WorkspaceLike",
    "build_default_tool_registry",
    "load_agent_suite_config",
    "load_general_agent_config",
    "register_agent_tool",
]
