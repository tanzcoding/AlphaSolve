from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


class SubagentDispatcher(Protocol):
    """??? Agent ???????????

    Why: ???????????? reasoning/compute/numerical ???
    subagent ??????????? SubagentService ?????
    subagent ??????????????????
    """
    def available_types(self) -> list[str]: ...
    def describe_type(self, agent_type: str) -> str: ...
    def call(self, agent_type: str, description: str, prompt: str, *, depth: int = ...) -> str: ...


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    stop_agent: bool = False
    stop_answer: str | None = None


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
