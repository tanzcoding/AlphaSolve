from __future__ import annotations

import random
import time
from typing import Any

import anthropic
from anthropic import Anthropic

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

_RETRYABLE_EXCEPTIONS = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
)


class AnthropicMessagesClient:
    def __init__(self, preset: Preset) -> None:
        self.preset = preset
        self.model = preset.model
        self.timeout = preset.timeout
        self.params = dict(preset.params)
        self.client = Anthropic(
            api_key=preset.resolve_api_key(),
            base_url=preset.base_url,
            timeout=self.timeout,
            max_retries=6,
        )

    def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDef],
        delta_sink: ChatDeltaSink | None = None,
    ) -> CompletionResponse:
        system_text, wire_messages = _messages_to_anthropic(messages)
        wire_tools = [_tool_to_anthropic(t) for t in tools] if tools else []

        request: dict[str, Any] = {
            "model": self.model,
            "messages": wire_messages,
            "max_tokens": int(self.params.get("max_tokens") or 8192),
            **{k: v for k, v in self.params.items() if k != "max_tokens"},
        }
        if system_text:
            request["system"] = system_text
        if wire_tools:
            request["tools"] = wire_tools

        max_retries = 8
        delay = 5.0
        for attempt in range(max_retries + 1):
            try:
                if delta_sink is not None:
                    return self._complete_streaming(request, delta_sink=delta_sink)
                return self._complete_non_streaming(request)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt == max_retries:
                    raise ChatCompletionError(
                        f"Anthropic request failed after {max_retries + 1} attempts: {exc}"
                    ) from exc
                time.sleep(delay + random.uniform(0, delay * 0.5))
                delay = min(delay * 2, 300.0)

        raise ChatCompletionError("unreachable retry state")

    def _complete_non_streaming(self, request: dict[str, Any]) -> CompletionResponse:
        response = self.client.messages.create(**request)
        return _anthropic_response_to_completion(response)

    def _complete_streaming(
        self, request: dict[str, Any], *, delta_sink: ChatDeltaSink
    ) -> CompletionResponse:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_blocks: dict[int, dict[str, Any]] = {}  # index -> {"id","name","input_str"}
        stop_reason = "end_turn"
        usage_in = 0
        usage_out = 0
        usage_cached = 0

        with self.client.messages.stream(**request) as stream:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        sr = getattr(delta, "stop_reason", None)
                        if sr:
                            stop_reason = str(sr)
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        usage_out += int(getattr(usage, "output_tokens", 0) or 0)
                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    index = int(getattr(event, "index", 0))
                    if block is not None and getattr(block, "type", "") == "tool_use":
                        tool_blocks[index] = {
                            "id": str(getattr(block, "id", "")),
                            "name": str(getattr(block, "name", "")),
                            "input_str": "",
                        }
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    dtype = getattr(delta, "type", "")
                    index = int(getattr(event, "index", 0))
                    if dtype == "text_delta":
                        text = str(getattr(delta, "text", "") or "")
                        if text:
                            text_parts.append(text)
                            delta_sink(StreamDelta(type="text", text=text))
                    elif dtype == "thinking_delta":
                        thinking = str(getattr(delta, "thinking", "") or "")
                        if thinking:
                            thinking_parts.append(thinking)
                            delta_sink(StreamDelta(type="reasoning", text=thinking))
                    elif dtype == "input_json_delta":
                        partial = str(getattr(delta, "partial_json", "") or "")
                        if partial and index in tool_blocks:
                            tool_blocks[index]["input_str"] += partial
                            delta_sink(StreamDelta(
                                type="tool_input",
                                tool_call_id=tool_blocks[index]["id"],
                                arg_delta=partial,
                            ))
                elif etype == "message_start":
                    msg_obj = getattr(event, "message", None)
                    if msg_obj is not None:
                        usage = getattr(msg_obj, "usage", None)
                        if usage is not None:
                            usage_in = int(getattr(usage, "input_tokens", 0) or 0)
                            usage_cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        # Build the final message
        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_blocks):
            block = tool_blocks[idx]
            try:
                args = _parse_json_strict(block["input_str"] or "{}", context=f"tool_call {block['id']}")
            except ChatCompletionError as exc:
                # Instead of crashing, create ToolCall with error info so the
                # agent loop can return a helpful message to the LLM.
                raw = block["input_str"] or "{}"
                smart_error = _smart_json_error_anthropic(raw, exc)
                tool_calls.append(ToolCall(
                    id=block["id"],
                    name=block["name"],
                    args={},
                    raw_args=raw,
                    parse_error=smart_error,
                ))
                continue
            tool_calls.append(ToolCall(id=block["id"], name=block["name"], args=args))

        reasoning_content = "".join(thinking_parts) if thinking_parts else ""
        msg = Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            reasoning_content=reasoning_content,
        )
        return CompletionResponse(
            message=msg,
            finish_reason=_normalize_stop_reason(stop_reason, has_tool_calls=bool(tool_calls)),
            usage=Usage(input_tokens=usage_in, output_tokens=usage_out, cached_tokens=usage_cached),
            raw=None,
        )


# ----- Conversion helpers -----

def _messages_to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Lift system to a string, fold role='tool' into the following user as tool_result."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_pending_into_user():
        nonlocal pending_tool_results
        if not pending_tool_results:
            return
        out.append({"role": "user", "content": pending_tool_results})
        pending_tool_results = []

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        if m.role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content,
            })
            continue
        # Before emitting a non-tool message, flush any accumulated tool results as a user message.
        flush_pending_into_user()
        if m.role == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.args,
                })
            out.append({"role": "assistant", "content": blocks})

    flush_pending_into_user()

    system_text = "\n\n".join(s for s in system_parts if s)
    return system_text, out


def _tool_to_anthropic(td: ToolDef) -> dict[str, Any]:
    return {
        "name": td.name,
        "description": td.description,
        "input_schema": td.parameters,
    }


def _anthropic_response_to_completion(response: Any) -> CompletionResponse:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in getattr(response, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            text_parts.append(str(getattr(block, "text", "") or ""))
        elif btype == "thinking":
            thinking_parts.append(str(getattr(block, "thinking", "") or ""))
        elif btype == "tool_use":
            tool_calls.append(ToolCall(
                id=str(getattr(block, "id", "")),
                name=str(getattr(block, "name", "")),
                args=dict(getattr(block, "input", {}) or {}),
            ))

    stop_reason = str(getattr(response, "stop_reason", "") or "end_turn")
    usage_obj = getattr(response, "usage", None)
    usage = Usage(
        input_tokens=int(getattr(usage_obj, "input_tokens", 0) or 0) if usage_obj else 0,
        output_tokens=int(getattr(usage_obj, "output_tokens", 0) or 0) if usage_obj else 0,
        cached_tokens=int(getattr(usage_obj, "cache_read_input_tokens", 0) or 0) if usage_obj else 0,
    )
    msg = Message(
        role="assistant",
        content="".join(text_parts),
        tool_calls=tuple(tool_calls),
        reasoning_content="".join(thinking_parts) if thinking_parts else "",
    )
    return CompletionResponse(
        message=msg,
        finish_reason=_normalize_stop_reason(stop_reason, has_tool_calls=bool(tool_calls)),
        usage=usage,
        raw=response,
    )


def _normalize_stop_reason(stop: str, *, has_tool_calls: bool) -> FinishReason:
    # Anthropic uses: end_turn / max_tokens / stop_sequence / tool_use
    if stop == "tool_use" or has_tool_calls:
        return "tool_calls"
    if stop == "max_tokens":
        return "length"
    if stop in ("end_turn", "stop_sequence"):
        return "stop"
    return "stop"


def _parse_json_strict(s: str, *, context: str) -> dict[str, Any]:
    import json as _json
    try:
        return _json.loads(s)
    except _json.JSONDecodeError as exc:
        raise ChatCompletionError(f"{context}: invalid JSON input from stream: {exc}; raw: {s!r}") from exc


def _smart_json_error_anthropic(raw: str, exc: ChatCompletionError) -> str:
    """Build a human-friendly JSON parse error message from a ChatCompletionError.

    The Anthropic provider wraps json.JSONDecodeError inside ChatCompletionError.
    We unwrap the original JSONDecodeError to get position info, then use the
    same smart-error logic as the OpenAI provider.
    """
    import json as _json

    original = exc.__cause__
    if isinstance(original, _json.JSONDecodeError):
        pos = original.pos
        msg = original.msg
        total = len(raw)

        ctx_radius = 50
        ctx_start = max(0, pos - ctx_radius)
        ctx_end = min(total, pos + ctx_radius)
        before = raw[ctx_start:pos]
        at_char = raw[pos] if pos < total else ""
        after = raw[pos:ctx_end]
        context_snippet = f"{before}>>>{at_char}<<<{after}"

        key_name = _find_json_key(raw, pos)

        lines = [f"JSON arguments parse error: {msg}"]
        if key_name:
            lines.append(f"The error appears to be in the value for key \"{key_name}\".")
        lines.append(f"Position {pos} of {total} total characters.")
        if pos >= total - 1 or (total > 0 and raw.rstrip()[-1] != '}'):
            lines.append("The arguments string ended unexpectedly (likely truncated mid-value).")
        lines.append(f"Context around the error:\n  {context_snippet}")

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

    # Fallback: no JSONDecodeError wrapped — just show the ChatCompletionError message
    return str(exc)


def _find_json_key(s: str, pos: int) -> str | None:
    """Try to identify which JSON key an error position falls under."""
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
