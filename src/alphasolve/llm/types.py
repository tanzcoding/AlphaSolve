from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "error"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str = ""
    # Why: some OpenAI-compat providers (DeepSeek, Volcano, ...) require the
    # assistant's chain-of-thought field to be echoed back unchanged next turn.


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


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
    # retry metadata
    attempt: int = 0
    error_type: str = ""
    error: str = ""
    error_detail: str = ""
    fallback: str = ""


ChatDeltaSink = Callable[[StreamDelta], None]


class ChatCompletionError(RuntimeError):
    """Raised on transport, malformed-response, or provider 5xx failures."""


class ChatClient(Protocol):
    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse: ...
