from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    raw_args: str | None = None      # 参数解析失败时保留原始 JSON。
    parse_error: str | None = None   # 参数解析失败的可读说明。


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """供日志、界面和 curator 使用的可见消息，不用于重建 Codex 会话。"""

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str = ""
    # 仅记录运行时公开的推理内容或摘要，不依赖模型的隐藏思维链。


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
