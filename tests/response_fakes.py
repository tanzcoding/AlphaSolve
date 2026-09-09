"""让已有场景脚本描述离线模型响应；这些类型不属于生产接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from alphasolve.llm.types import Message, Usage


FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]


@dataclass(frozen=True)
class CompletionResponse:
    message: Message
    finish_reason: FinishReason
    usage: Usage = field(default_factory=Usage)
    raw: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class StreamDelta:
    type: Literal["text", "reasoning", "tool_input", "retry"]
    text: str = ""
    tool_call_id: str = ""
    arg_delta: str = ""
    attempt: int = 0
    error_type: str = ""
    error: str = ""
    error_detail: str = ""
    fallback: str = ""


ChatDeltaSink = Callable[[StreamDelta], None]


class ChatCompletionError(RuntimeError):
    """离线测试脚本模拟模型请求失败。"""
