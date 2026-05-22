"""ClientFactory 类型别名：把 AgentConfig 映射到 ChatClient 的工厂。"""
from __future__ import annotations

from typing import Any, Callable

from alphasolve.agent import AgentConfig

ClientFactory = Callable[[AgentConfig], Any]
