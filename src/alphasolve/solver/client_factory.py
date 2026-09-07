"""ClientFactory 类型别名：根据角色配置选择 Codex 会话的模型配置。"""
from __future__ import annotations

from typing import Any, Callable

from alphasolve.agent import AgentConfig

ClientFactory = Callable[[AgentConfig], Any]
