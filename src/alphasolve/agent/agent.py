from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from alphasolve.llm.client import CodexClient
from alphasolve.llm.types import Message, ToolCall

from .codex_session import CodexSession, ToolRequest, classify_failure
from .config import AgentConfig
from .tools import ToolRegistry, ToolResult


AgentEventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class AgentRunResult:
    final_answer: str
    messages: list[Message]
    trace: list[dict[str, Any]]
    turns: int


class AgentRunError(RuntimeError):
    def __init__(self, message: str, *, trace: list[dict[str, Any]], failure_kind: str = "runtime"):
        super().__init__(message)
        self.trace = trace
        self.failure_kind = failure_kind
        self.fatal = failure_kind in {"quota", "auth", "configuration"}


class Agent:
    """角色工具与可见 trace 的适配器；原生会话和上下文压缩交给 Codex。"""

    def __init__(self, *, config: AgentConfig, client: CodexClient, tool_registry: ToolRegistry,
                 event_sink: AgentEventSink | None = None, stop_event: threading.Event | None = None,
                 caller_context: dict[str, Any] | None = None):
        self.config = config
        self.client = client
        self.tool_registry = tool_registry
        self.event_sink = event_sink
        self.stop_event = stop_event
        self.caller_context = dict(caller_context or {})
        self.last_trace: list[dict[str, Any]] = []
        self._session: CodexSession | None = None
        self._usage_total = {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0}
        self._turn = 0

    def _make_session(self) -> CodexSession:
        return CodexSession(self.client.preset, self.config.system_prompt, self.tool_registry.tool_defs(
            self.config.tools, self.config.tool_parameters, self.config.tool_descriptions))

    def _emit(self, event: dict[str, Any]) -> None:
        if self.caller_context:
            event["caller_context"] = self.caller_context
        if self.event_sink:
            try:
                self.event_sink(event)
            except Exception:
                pass

    def run(self, task: str, *, description: str = "") -> AgentRunResult:
        self._turn += 1
        turn = self._turn
        started = time.monotonic()
        trace: list[dict[str, Any]] = []
        messages = [Message(role="system", content=self.config.system_prompt), Message(role="user", content=task)]
        self.last_trace = trace
        final_answer = ""
        usage = dict(self._usage_total)
        flushed = False
        visible_streams: dict[tuple[str, str | None], dict[int, str]] = {}
        completed_items: set[tuple[str, str]] = set()

        def record(kind: str, **values: Any) -> None:
            event = {"type": kind, "agent": self.config.name, "turn": turn, **values}
            trace.append(event)
            self._emit(event)

        def stream_content(parts: dict[int, str]) -> str:
            return "\n".join(parts[index] for index in sorted(parts))

        def completed_content(kind: str, content: str, item_id: str | None) -> str:
            if content:
                return content
            parts = visible_streams.get((kind, item_id))
            if parts is None:
                parts = visible_streams.get((kind, None), {})
            # 完成通知可能省略正文；已收到的可见增量仍属于这一项。
            return stream_content(parts)

        def emit_delta(kind: str, *, content: str, delta: str, item_id: str | None) -> None:
            if delta:
                self._emit({"type": kind + "_delta", "agent": self.config.name, "turn": turn,
                            "content": content, "delta": delta, "item_id": item_id,
                            "elapsed": time.monotonic() - started})

        def finish_visible(kind: str, content: str, item_id: str | None, *, partial: bool = False) -> None:
            stream_item_id = item_id
            parts = visible_streams.pop((kind, item_id), None)
            if parts is None:
                # 兼容没有 itemId 的事件源；原生会话仍按各自的 item 隔离增量。
                parts = visible_streams.pop((kind, None), None)
                if parts is not None:
                    stream_item_id = None
                else:
                    parts = {}
            streamed = stream_content(parts)
            if content.startswith(streamed):
                # 某些项只发送完成事件，或只发送部分增量；补齐正文后再结束这一项。
                emit_delta(kind, content=content, delta=content[len(streamed):], item_id=stream_item_id)
                delivered = True
            else:
                delivered = False
            if kind == "thinking":
                if content:
                    record("thinking", content=content, streamed=delivered, item_id=item_id,
                           partial=partial, elapsed=time.monotonic() - started)
            else:
                record("assistant_message", content=content, tool_call_count=0,
                       streamed_content=delivered, item_id=item_id, partial=partial,
                       stream_item_id=stream_item_id,
                       replace_content=streamed if not delivered else None,
                       raw={"role": "assistant", "content": content})

        def flush() -> None:
            nonlocal flushed
            if flushed:
                return
            flushed = True
            elapsed = time.monotonic() - started
            # 异常或中断时保留已经可见的片段，不重复发送已经完成的项。
            for (kind, item_id), parts in list(visible_streams.items()):
                finish_visible(kind, stream_content(parts), item_id, partial=True)
            record("usage", input_tokens=max(0, usage.get("inputTokens", 0) - self._usage_total["inputTokens"]),
                   output_tokens=max(0, usage.get("outputTokens", 0) - self._usage_total["outputTokens"]),
                   cached_tokens=max(0, usage.get("cachedInputTokens", 0) - self._usage_total["cachedInputTokens"]), elapsed=elapsed)
            self._usage_total = usage

        record("run_start", task=task, description=description, enabled_tools=list(self.config.tools), tool_parameters=self.config.tool_parameters)
        try:
            if self.stop_event is not None and self.stop_event.is_set():
                record("run_stopped", reason="stop_event set")
                return AgentRunResult("", messages, trace, 0)
            if self._session is None:
                self._session = self._make_session()
            record("model_request")
            self._session.start_turn(task)
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    flush()
                    record("run_stopped", reason="stop_event set")
                    self.close()
                    return AgentRunResult("", messages, trace, 1)
                try:
                    event = self._session.events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self.stop_event is not None and self.stop_event.is_set():
                    flush()
                    record("run_stopped", reason="stop_event set")
                    self.close()
                    return AgentRunResult("", messages, trace, 1)
                if isinstance(event, ToolRequest):
                    params = event.params
                    name = params["tool"]
                    call_id = params["callId"]
                    args = params.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = None
                    record("tool_call", name=name, tool_call_id=call_id, arguments=args)
                    call = ToolCall(id=call_id, name=name, args=args if isinstance(args, dict) else {})
                    messages.append(Message(role="assistant", tool_calls=(call,)))
                    if not isinstance(args, dict):
                        result = ToolResult("Tool arguments must be a JSON object; please retry.", is_error=True)
                    else:
                        # 留在当前线程：子代理的委派预算与调用上下文使用 threading.local。
                        result = self.tool_registry.execute(name, args, enabled=list(self.config.tools), tool_parameters=self.config.tool_parameters)
                    record("tool_result", name=name, tool_call_id=call_id, content=result.content,
                           is_error=result.is_error, stop_agent=result.stop_agent)
                    messages.append(Message(role="tool", name=name, tool_call_id=call_id, content=result.content))
                    if not event.response.done():
                        event.response.set_result({"contentItems": [{"type": "inputText", "text": result.content}], "success": not result.is_error})
                    if result.stop_agent:
                        final_answer = result.stop_answer or result.content
                        flush()
                        record("run_finish", final_answer=final_answer, reason="tool_requested_stop")
                        self.close()
                        return AgentRunResult(final_answer, messages, trace, 1)
                    continue
                method, data = event
                if method == "_failure":
                    raise data["error"]
                if method == "thread/tokenUsage/updated":
                    usage = data["tokenUsage"]["total"]
                elif method in {"item/agentMessage/delta", "item/reasoning/summaryTextDelta"}:
                    kind = "assistant" if method == "item/agentMessage/delta" else "thinking"
                    item_id = data.get("itemId")
                    if item_id is not None and (kind, item_id) in completed_items:
                        continue
                    parts = visible_streams.setdefault((kind, item_id), {})
                    before = stream_content(parts)
                    index = int(data.get("summaryIndex") or 0) if kind == "thinking" else 0
                    parts[index] = parts.get(index, "") + data.get("delta", "")
                    content = stream_content(parts)
                    delta = content[len(before):] if content.startswith(before) else data.get("delta", "")
                    emit_delta(kind, content=content, delta=delta, item_id=item_id)
                elif method == "item/completed":
                    item = data["item"]
                    item_id = item.get("id")
                    kind = {"agentMessage": "assistant", "reasoning": "thinking"}.get(item["type"])
                    if kind is not None and item_id is not None:
                        if (kind, item_id) in completed_items:
                            continue
                        completed_items.add((kind, item_id))
                    if item["type"] == "agentMessage":
                        text = completed_content("assistant", item.get("text", ""), item_id)
                        messages.append(Message(role="assistant", content=text))
                        record("codex_message", content=text, phase=item.get("phase"), item_id=item_id)
                        finish_visible("assistant", text, item_id)
                        if item.get("phase") != "commentary":
                            final_answer = text
                    elif item["type"] == "reasoning":
                        summary = item.get("summary", [])
                        parts = [part if isinstance(part, str) else part.get("text", "") for part in summary]
                        content = completed_content("thinking", "\n".join(parts), item_id)
                        if content:
                            record("visible_reasoning", content=content, item_id=item_id)
                        finish_visible("thinking", content, item_id)
                    elif item["type"] == "contextCompaction":
                        record("context_compacted")
                elif method == "thread/compacted":
                    record("context_compacted")
                elif method == "error":
                    record("codex_error", **data)
                elif method == "turn/completed":
                    native_turn = data["turn"]
                    flush()
                    if native_turn["status"] == "failed":
                        error = native_turn.get("error") or {"message": "Codex turn failed"}
                        raise AgentRunError(str(error), trace=trace, failure_kind=classify_failure(error))
                    if native_turn["status"] == "interrupted":
                        record("run_stopped", reason="Codex turn interrupted")
                    else:
                        record("run_finish", final_answer=final_answer)
                    return AgentRunResult(final_answer, messages, trace, 1)
        except KeyboardInterrupt:
            flush()
            record("run_stopped", reason="keyboard_interrupt")
            self.close()
            return AgentRunResult("", messages, trace, 1)
        except Exception as exc:
            flush()
            kind = classify_failure(exc)
            record("run_error", error_type=type(exc).__name__, error=str(exc), failure_kind=kind)
            self.close()
            raise AgentRunError(f"agent {self.config.name} failed: {exc}", trace=trace, failure_kind=kind) from exc

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
            self._usage_total = {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0}

    def __enter__(self) -> Agent:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
