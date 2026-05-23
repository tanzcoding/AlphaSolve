"""第二层：通用 coding agent runtime。

依赖契约：
- 允许 import: alphasolve.llm.*（公共 API）、alphasolve.utils.*
- 禁止 import: alphasolve.workflow.*（反向依赖）
- 禁止 import: alphasolve.solver.*（execution 是 solver 域的）
- 禁止 import: alphasolve.llm.providers.*、alphasolve.llm.config.*（实现细节）

公共 API（__all__）：见下。LLM 类型（Message/ChatClient/ToolDef/...）继续从
alphasolve.llm 拿，第二层不 re-export。
"""
from .agent import Agent, AgentRunResult, AgentRunError, AgentEventSink
from .config import (
    AgentConfig, AgentSuite,
    load_agent_config, load_agent_suite,
)
from .tools import (
    ToolRegistry, ToolResult, RegisteredTool,
    build_default_tool_registry,
    SubagentDispatcher, register_agent_tool,
)
from .workspace import Workspace, WorkspaceLike, PagedReadResult

__all__ = [
    # Agent runtime
    "Agent", "AgentRunResult", "AgentRunError", "AgentEventSink",
    # Config
    "AgentConfig", "AgentSuite", "load_agent_config", "load_agent_suite",
    # Tools
    "ToolRegistry", "ToolResult", "RegisteredTool",
    "build_default_tool_registry",
    "SubagentDispatcher", "register_agent_tool",
    # Workspace
    "Workspace", "WorkspaceLike", "PagedReadResult",
]
