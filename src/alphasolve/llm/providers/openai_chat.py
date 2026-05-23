from __future__ import annotations

import json
import random
import time
import traceback
from typing import Any

import httpx
import openai
from openai import OpenAI

from ..config.preset import Preset
from ..types import (
    ChatClient,
    ChatCompletionError,
    ChatDeltaSink,
    CompletionResponse,
    FinishReason,
    Message,
    StreamDelta,
    ToolCall,
    ToolDef,
    Usage,
)

_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text", "thinking")
_MISSING = object()

_RETRYABLE_EXCEPTIONS = (
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    httpx.RemoteProtocolError,
)


class OpenAIChatClient:
    _STREAMING_MAX_RETRIES = 3

    def __init__(self, preset: Preset, *, http_client: httpx.Client | None = None) -> None:
        self.preset = preset
        self.model = preset.model
        self.timeout = preset.timeout
        self.params = dict(preset.params)
        self.temperature = self.params.pop("temperature", 1.0)
        self.thinking_mode = _config_enables_thinking(self.params)
        self.client = OpenAI(
            api_key=preset.resolve_api_key(),
            base_url=preset.base_url,
            timeout=self.timeout,
            max_retries=6,
            http_client=http_client,
        )

    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_to_openai(messages, thinking_mode=self.thinking_mode),
            "temperature": self.temperature,
            **self.params,
        }
        if tools:
            request["tools"] = [_tool_to_openai(t) for t in tools]

        max_retries = 8
        delay = 5.0
        streaming_failures = 0
        use_streaming = delta_sink is not None

        for attempt in range(max_retries + 1):
            try:
                if use_streaming:
                    raw_message = self._complete_streaming(request, delta_sink=delta_sink)
                else:
                    raw_message = self._complete_non_streaming(request, delta_sink=delta_sink)
                return _openai_response_to_completion(raw_message)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt == max_retries:
                    raise ChatCompletionError(
                        f"OpenAI request failed after {max_retries + 1} attempts: {exc}"
                    ) from exc
                if use_streaming:
                    streaming_failures += 1
                    if streaming_failures >= self._STREAMING_MAX_RETRIES:
                        use_streaming = False
                        if delta_sink is not None:
                            delta_sink(StreamDelta(
                                type="retry",
                                attempt=attempt + 1,
                                error_type=type(exc).__name__,
                                error=str(exc),
                                error_detail=_format_exception_detail(exc),
                                fallback="non_streaming",
                            ))
                        delay = 5.0
                        continue
                if delta_sink is not None:
                    delta_sink(StreamDelta(
                        type="retry",
                        attempt=attempt + 1,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        error_detail=_format_exception_detail(exc),
                    ))
                time.sleep(delay + random.uniform(0, delay * 0.5))
                delay = min(delay * 2, 300.0)

        raise ChatCompletionError("unreachable retry state")

    def _complete_non_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink | None
    ) -> dict[str, Any]:
        response = self.client.chat.completions.create(**request)
        message = _object_to_dict(response.choices[0].message)
        finish_reason = str(response.choices[0].finish_reason or "stop")
        message["_finish_reason"] = finish_reason
        message["_usage"] = _object_to_dict(getattr(response, "usage", None) or {})
        message["_raw"] = response
        if delta_sink is not None:
            reasoning = str(message.get("reasoning_content") or "")
            if reasoning:
                delta_sink(StreamDelta(type="reasoning", text=reasoning))
            content = str(message.get("content") or "")
            if content:
                delta_sink(StreamDelta(type="text", text=content))
        return message

    def _complete_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink
    ) -> dict[str, Any]:
        stream_request = dict(request)
        stream_request["stream"] = True

        role = "assistant"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        finish_reason: str = "stop"

        for chunk in self.client.chat.completions.create(**stream_request):
            chunk_dict = _object_to_dict(chunk)
            choices = chunk_dict.get("choices") or []
            if not choices:
                continue
            choice = _object_to_dict(choices[0])
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            delta = _object_to_dict(choice.get("delta") or {})
            if not delta:
                continue

            role = str(delta.get("role") or role)
            content_delta = _first_text_delta(delta, ("content",))
            if content_delta:
                content_parts.append(content_delta)
                delta_sink(StreamDelta(type="text", text=content_delta))
            reasoning_delta = _first_text_delta(delta, _REASONING_KEYS)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                delta_sink(StreamDelta(type="reasoning", text=reasoning_delta))

            for raw_tool_delta in delta.get("tool_calls") or []:
                tool_delta = _object_to_dict(raw_tool_delta)
                index = int(tool_delta.get("index") or 0)
                current = tool_call_parts.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tool_delta.get("id"):
                    current["id"] = str(tool_delta["id"])

                function_delta = _object_to_dict(tool_delta.get("function") or {})
                function = current.setdefault("function", {"name": "", "arguments": ""})
                if function_delta.get("name"):
                    function["name"] = str(function.get("name") or "") + str(function_delta["name"])
                if function_delta.get("arguments"):
                    arg_chunk = str(function_delta["arguments"])
                    function["arguments"] = str(function.get("arguments") or "") + arg_chunk
                    delta_sink(StreamDelta(type="tool_input", tool_call_id=current["id"], arg_delta=arg_chunk))

        message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_call_parts:
            message["tool_calls"] = [tool_call_parts[i] for i in sorted(tool_call_parts)]
        message["_finish_reason"] = finish_reason
        message["_usage"] = {}
        message["_raw"] = None
        return message


# ----- Conversion helpers (boundary code; not part of the public API) -----

def _config_enables_thinking(params: dict[str, Any]) -> bool:
    extra_body = params.get("extra_body") if isinstance(params, dict) else None
    if not isinstance(extra_body, dict):
        return False
    if extra_body.get("enable_thinking") is True:
        return True
    reasoning = extra_body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning:
        return True
    thinking = extra_body.get("thinking")
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").lower()
        return thinking_type in {"enabled", "enable", "on", "true"} or bool(thinking.get("enabled"))
    return False


def _first_text_delta(delta: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = delta.get(key)
        if value:
            return str(value)
    return ""


def _object_to_dict(value: Any) -> dict[str, Any]:
    """Convert a possibly-pydantic SDK object to a dict.

    Restores two historical bug fixes from the pre-refactor OpenAIChatClient:
      (1) If `model_dump` omits a reasoning key the SDK still exposes as an
          attribute (some SDK versions don't declare reasoning_content in their
          model schema), recover it via `getattr`.
      (2) Normalise provider-specific reasoning aliases (`thinking`,
          `reasoning_text`, `reasoning`) into the canonical `reasoning_content`
          key so downstream code only has to look in one place.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return _normalize_message_reasoning(dict(value))
    if hasattr(value, "model_dump"):
        result = value.model_dump(exclude_none=False)
        for attr in _REASONING_KEYS:
            if attr in result or not hasattr(value, attr):
                continue
            attr_value = getattr(value, attr)
            if attr_value is not None:
                result[attr] = str(attr_value)
        return _normalize_message_reasoning(result)
    if hasattr(value, "dict"):
        return _normalize_message_reasoning(value.dict())
    return _normalize_message_reasoning(dict(value)) if hasattr(value, "__iter__") else {"value": value}


def _normalize_message_reasoning(message: dict[str, Any]) -> dict[str, Any]:
    """Alias-normalise reasoning keys → reasoning_content."""
    normalized = dict(message)
    reasoning = _first_present_reasoning_value(normalized)
    if reasoning is not _MISSING and "reasoning_content" not in normalized:
        normalized["reasoning_content"] = "" if reasoning is None else str(reasoning)
    return normalized


def _first_present_reasoning_value(message: dict[str, Any]) -> Any:
    for key in _REASONING_KEYS:
        if key in message:
            return message[key]
    return _MISSING


def _messages_to_openai(messages: list[Message], *, thinking_mode: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({
                "role": "tool",
                "content": m.content,
                "tool_call_id": m.tool_call_id or "",
                "name": m.name or "",
            })
            continue
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.args, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        if m.reasoning_content:
            d["reasoning_content"] = m.reasoning_content
        elif thinking_mode and m.role == "assistant" and m.tool_calls and "reasoning_content" not in d:
            # Historical bug fix: thinking-mode providers reject the next request
            # if a previous assistant message carrying tool_calls is missing
            # reasoning_content. Inject an empty string so the field is present.
            d["reasoning_content"] = ""
        out.append(d)
    return out


def _tool_to_openai(td: ToolDef) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": td.name,
            "description": td.description,
            "parameters": td.parameters,
        },
    }


def _openai_response_to_completion(raw_message: dict[str, Any]) -> CompletionResponse:
    finish_reason = raw_message.pop("_finish_reason", "stop")
    usage_dict = raw_message.pop("_usage", {})
    raw_obj = raw_message.pop("_raw", None)

    tool_calls: list[ToolCall] = []
    for tc_raw in raw_message.get("tool_calls") or []:
        fn = tc_raw.get("function") or {}
        args_str = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str)
        except json.JSONDecodeError as exc:
            raise ChatCompletionError(
                f"tool call {tc_raw.get('id', '?')} returned invalid JSON args: {exc}; raw: {args_str!r}"
            ) from exc
        tool_calls.append(ToolCall(id=str(tc_raw.get("id") or ""), name=str(fn.get("name") or ""), args=args))

    msg = Message(
        role="assistant",
        content=str(raw_message.get("content") or ""),
        tool_calls=tuple(tool_calls),
        reasoning_content=str(raw_message.get("reasoning_content") or ""),
    )
    usage = Usage(
        input_tokens=int(usage_dict.get("prompt_tokens") or usage_dict.get("input_tokens") or 0),
        output_tokens=int(usage_dict.get("completion_tokens") or usage_dict.get("output_tokens") or 0),
        cached_tokens=int(usage_dict.get("cache_read_input_tokens") or usage_dict.get("cached_tokens") or 0),
    )
    fr: FinishReason = finish_reason if finish_reason in {"stop", "tool_calls", "length", "content_filter", "error"} else "stop"
    return CompletionResponse(message=msg, finish_reason=fr, usage=usage, raw=raw_obj)


def _format_exception_detail(exc: BaseException) -> str:
    lines = [item.strip() for item in traceback.format_exception_only(type(exc), exc) if item.strip()]
    detail = " ".join(lines)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        cause_lines = [item.strip() for item in traceback.format_exception_only(type(cause), cause) if item.strip()]
        cause_detail = " ".join(cause_lines)
        if cause_detail and cause_detail not in detail:
            detail = f"{detail} | caused by {cause_detail}" if detail else cause_detail
    return detail
