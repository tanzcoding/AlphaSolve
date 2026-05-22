"""Workflow 层的工具 registry 工厂：直接调用第二层 build_default_tool_registry。

历史上这里重复注册了 12 个基础工具（Read / Write / Edit / ...），与第二层的
``alphasolve.agent.tool_registry.build_default_tool_registry`` 一字不差地重复。
A 阶段 Task 8 把所有差异化措辞（"Results respect this agent's workspace access
restrictions." 等）搬到每个 agent YAML 的 ``tool_descriptions`` 字段，本文件
退化为薄包装。

``allow_write`` / ``allow_manage`` / ``allow_delete`` / ``subagent_service``
参数保留是为了向后兼容现有 call sites；它们不再影响工具集（每个 agent
能用哪些工具由 YAML 的 ``tools:`` 白名单决定）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from alphasolve.agent import ToolRegistry
from alphasolve.agent.tool_registry import build_default_tool_registry, register_agent_tool

from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from .subagent_service import SubagentService


__all__ = ["build_workspace_tool_registry", "register_agent_tool"]


def build_workspace_tool_registry(
    access: RoleWorkspaceAccess,
    *,
    allow_write: bool = False,  # 兼容旧调用方；不再影响工具集
    allow_manage: bool = False,
    allow_delete: bool = False,
    subagent_service: "SubagentService | None" = None,
) -> ToolRegistry:
    """构造 workflow agent 用的工具 registry。

    与第二层完全一致：直接调 ``build_default_tool_registry(access)``。
    第三层不再有自己的基础工具注册；agent 能用哪些工具由 YAML 的 ``tools:``
    白名单决定，差异化措辞由 YAML 的 ``tool_descriptions`` 字段表达。
    """
    return build_default_tool_registry(access)
