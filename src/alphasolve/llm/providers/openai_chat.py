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
        # 让 OpenAI 兼容后端在流末尾额外回一个带 usage 的 chunk（否则流式 usage 为空，
        # token 统计会全 0）。DeepSeek/OpenAI 均支持该选项。
        stream_request["stream_options"] = {"include_usage": True}

        role = "assistant"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        finish_reason: str = "stop"
        usage_captured: dict[str, Any] = {}

        for chunk in self.client.chat.completions.create(**stream_request):
            chunk_dict = _object_to_dict(chunk)
            chunk_usage = chunk_dict.get("usage")
            if chunk_usage:
                usage_captured = _object_to_dict(chunk_usage)
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
            # Some providers (DeepSeek) may not send a tool_call ``id`` in the
            # streaming delta, or may send it only in the first chunk for the
            # first tool call and omit it for subsequent ones. An empty or
            # duplicate ``id`` causes the API to reject the next request with
            # "Messages with role 'tool' must be a response to a preceding
            # message with 'tool_calls'" or "insufficient tool messages
            # following tool_calls". Backfill any missing/duplicate ids so the
            # message sequence stays valid. The same _next_backfill_id() used by
            # _messages_to_openai ensures consistency between streaming and
            # non-streaming paths.
            tool_calls_list = [tool_call_parts[i] for i in sorted(tool_call_parts)]
            seen_ids: set[str] = set()
            for tc in tool_calls_list:
                tc_id = tc.get("id") or ""
                if not tc_id or tc_id in seen_ids:
                    tc_id = _next_backfill_id()
                seen_ids.add(tc_id)
                tc["id"] = tc_id
            message["tool_calls"] = tool_calls_list
        message["_finish_reason"] = finish_reason
        message["_usage"] = usage_captured
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


_BACKFILL_ID_COUNTER = [0]


def _next_backfill_id() -> str:
    """Generate a stable, unique tool_call id for providers that omit it.

    Uses a module-level monotonic counter so ids are unique across calls within
    a process, avoiding collisions that ``id(tc)`` (object address) can suffer
    from after garbage collection reuses memory.
    """
    _BACKFILL_ID_COUNTER[0] += 1
    return f"call_backfill_{_BACKFILL_ID_COUNTER[0]}"


def _messages_to_openai(messages: list[Message], *, thinking_mode: bool) -> list[dict[str, Any]]:
    """Serialize ``Message`` list to OpenAI chat-completions format.

    This function also **repairs the message sequence** to prevent 400 errors
    from OpenAI-compatible providers (especially DeepSeek) whose streaming
    deltas may omit or mis-time ``tool_call.id``:

    1. Every ``assistant`` message with ``tool_calls`` gets non-empty, unique
       ids on each tool call (backfilling empties).
    2. Every ``tool`` message's ``tool_call_id`` is reconciled to match the id
       of the corresponding tool call in the preceding assistant message.
    3. If an assistant message has *N* tool calls but fewer than *N* following
       ``tool`` messages (because the agent loop exited early or a tool was
       skipped), placeholder ``tool`` messages are appended so every
       ``tool_call_id`` has a response — otherwise the API rejects with
       "insufficient tool messages following tool_calls".
    4. Orphan ``tool`` messages (no preceding ``assistant`` with matching
       ``tool_calls``) are dropped, preventing "Messages with role 'tool' must
       be a response to a preceding message with 'tool_calls'".
    """
    out: list[dict[str, Any]] = []
    # Track the tool_call ids declared by the most recent assistant message
    # that carried tool_calls.  ``tool`` messages that follow must respond to
    # exactly these ids, in order.
    pending_tc_ids: list[str] | None = None  # ids awaiting tool responses
    pending_tc_consumed: set[str] = set()     # ids already responded to

    for m in messages:
        if m.role == "tool":
            # If there are no pending tool_calls to respond to, this tool
            # message is an orphan — drop it to avoid a 400 error.
            if not pending_tc_ids:
                continue
            # Find the matching tool_call id.  Prefer the stored tool_call_id
            # if it matches one of the pending ids; otherwise assign the next
            # un-consumed pending id (positional fallback for providers whose
            # ids were lost in streaming).
            tc_id = m.tool_call_id or ""
            if tc_id in pending_tc_ids and tc_id not in pending_tc_consumed:
                resolved_id = tc_id
            else:
                # Positional fallback: take the first un-consumed id.
                resolved_id = next(
                    (tid for tid in pending_tc_ids if tid not in pending_tc_consumed),
                    pending_tc_ids[-1],  # shouldn't happen, but be safe
                )
            pending_tc_consumed.add(resolved_id)
            out.append({
                "role": "tool",
                "content": m.content,
                "tool_call_id": resolved_id,
                "name": m.name or "",
            })
            continue

        # Before emitting a new assistant/user message, if the previous
        # assistant had tool_calls that were not fully responded to, inject
        # placeholder tool messages for the missing responses.
        if pending_tc_ids and len(pending_tc_consumed) < len(pending_tc_ids):
            for tid in pending_tc_ids:
                if tid not in pending_tc_consumed:
                    out.append({
                        "role": "tool",
                        "content": "",
                        "tool_call_id": tid,
                        "name": "",
                    })
        pending_tc_ids = None
        pending_tc_consumed = set()

        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            tc_list = []
            seen_ids: set[str] = set()
            for tc in m.tool_calls:
                tc_id = tc.id
                if not tc_id or tc_id in seen_ids:
                    tc_id = _next_backfill_id()
                seen_ids.add(tc_id)
                tc_list.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.args, ensure_ascii=False),
                    },
                })
            d["tool_calls"] = tc_list
            # Record the ids so subsequent tool messages can be reconciled.
            pending_tc_ids = [tc["id"] for tc in tc_list]
            pending_tc_consumed = set()
        if m.reasoning_content:
            d["reasoning_content"] = m.reasoning_content
        elif thinking_mode and m.role == "assistant" and m.tool_calls and "reasoning_content" not in d:
            # Historical bug fix: thinking-mode providers reject the next request
            # if a previous assistant message carrying tool_calls is missing
            # reasoning_content. Inject an empty string so the field is present.
            d["reasoning_content"] = ""
        out.append(d)

    # If the very last assistant message had tool_calls without responses,
    # backfill placeholders at the tail too.
    if pending_tc_ids and len(pending_tc_consumed) < len(pending_tc_ids):
        for tid in pending_tc_ids:
            if tid not in pending_tc_consumed:
                out.append({
                    "role": "tool",
                    "content": "",
                    "tool_call_id": tid,
                    "name": "",
                })

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
            # Instead of crashing the agent, create a ToolCall with error info
            # so the agent loop can return a helpful message to the LLM.
            smart_error = _smart_json_error(args_str, exc)
            tool_calls.append(ToolCall(
                id=str(tc_raw.get("id") or ""),
                name=str(fn.get("name") or ""),
                args={},
                raw_args=args_str,
                parse_error=smart_error,
            ))
            continue
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


def _find_json_key(s: str, pos: int) -> str | None:
    """Try to identify which JSON key an error position falls under.

    Walks backwards from *pos* looking for the most recent "key": pattern
    at the same nesting depth.  Returns None if it can't determine the key.
    """
    depth = 0
    for i in range(pos - 1, -1, -1):
        ch = s[i]
        if ch == '}':
            depth += 1
        elif ch == '{':
            if depth > 0:
                depth -= 1
            else:
                break
        elif ch == ':' and depth == 0:
            # Found a top-level colon — extract the quoted key before it
            j = i - 1
            while j > 0 and s[j] in (' ', '\t', '\n'):
                j -= 1
            if j > 0 and s[j] == '"':
                quote_end = j
                j -= 1
                while j >= 0 and s[j] != '"':
                    j -= 1
                if j >= 0 and s[j] == '"':
                    return s[j + 1:quote_end]
    return None


def _smart_json_error(args_str: str, exc: json.JSONDecodeError) -> str:
    """Build a human-friendly JSON parse error message with surrounding context.

    Instead of just reporting "Unterminated string at char 60", shows the
    context around the error position and explains what went wrong so the
    LLM can understand and correct the mistake.
    """
    pos = exc.pos
    msg = exc.msg
    total = len(args_str)

    # Show ~50 chars of context on each side of the error position
    ctx_radius = 50
    ctx_start = max(0, pos - ctx_radius)
    ctx_end = min(total, pos + ctx_radius)
    before = args_str[ctx_start:pos]
    at_char = args_str[pos] if pos < total else ""
    after = args_str[pos:ctx_end]
    context_snippet = f"{before}>>>{at_char}<<<{after}"

    # Try to identify which key the error falls under
    key_name = _find_json_key(args_str, pos)

    # Build the message
    lines = [f"JSON arguments parse error: {msg}"]
    if key_name:
        lines.append(f"The error appears to be in the value for key \"{key_name}\".")
    lines.append(f"Position {pos} of {total} total characters.")

    # Detect truncation (error at/near end, or object never closed)
    if pos >= total - 1 or (total > 0 and args_str.rstrip()[-1] != '}'):
        lines.append("The arguments string ended unexpectedly (likely truncated mid-value).")

    lines.append(f"Context around the error:\n  {context_snippet}")

    # Targeted advice by error type
    lower_msg = msg.lower()
    if "unterminated string" in lower_msg:
        lines.append(
            "A string value was not closed with a quote mark. "
            "Regenerate the complete tool call with all string values properly "
            "quoted and the JSON object closed with }."
        )
    elif "expecting" in lower_msg or "unexpected" in lower_msg:
        lines.append(
            "The JSON structure has a syntax error at the marked position. "
            "Regenerate the tool call with valid JSON arguments."
        )
    else:
        lines.append("Regenerate the tool call with valid JSON arguments.")

    return "\n".join(lines)
