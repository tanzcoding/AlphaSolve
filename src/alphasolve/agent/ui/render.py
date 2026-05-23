"""第二层自己的 UI：渲染单 agent 生命周期事件（thinking/tool_call/assistant_message 等）。

phase C T10 后 --agent CLI 直接走 alphasolve.agent.ui.cli_app 里的 AgentApp（无 renderer，
LLM 输出直接 print 在 stdout）。具体的"单 agent 富 UI 渲染类"延后到 phase E：届时如要
重做富 UI，可以挪进本文件，并复用 ._render_shared.RICH_CONSOLE。
"""
from __future__ import annotations

from ._render_shared import RICH_CONSOLE


__all__ = ["RICH_CONSOLE"]
