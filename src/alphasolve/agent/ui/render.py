"""第二层自己的 UI：渲染单 agent 生命周期事件（thinking/tool_call/assistant_message 等）。

phase C 拆分时 rich_renderer.py 中真正"单 agent 事件渲染"的逻辑实际位于
alphasolve.workflow.debug_agent 里（Task 10 会重写成 agent/ui/cli_app.py）。
本文件目前仅做 import 转发，让 agent 层有正确的 ui 入口；具体的单 agent
渲染类（DebugAgentRenderer 等）由 Task 10 引入。
"""
from __future__ import annotations

from ._render_shared import RICH_CONSOLE


__all__ = ["RICH_CONSOLE"]
