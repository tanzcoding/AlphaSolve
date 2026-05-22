"""Re-export shim. 4 个职责文件的统一入口（A 阶段 commit 4 拆分）。

历史上这是 1203 行的杂烩文件；本文件保留是为 commit 4 → commit 8 之间
所有调用方继续工作；commit 8 之后会删除。
"""
from alphasolve.agent import GeneralAgentConfig  # 旧调用方仍从 tools 拿（如 orchestrator.py:612）

from .client_factory import ClientFactory
from .subagent_service import SubagentService
from .workflow_tools import build_workspace_tool_registry, register_agent_tool
from .workspace_access import RoleWorkspaceAccess

__all__ = [
    "ClientFactory",
    "GeneralAgentConfig",
    "RoleWorkspaceAccess",
    "SubagentService",
    "build_workspace_tool_registry",
    "register_agent_tool",
]
