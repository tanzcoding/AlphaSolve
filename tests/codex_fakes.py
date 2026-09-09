"""把离线场景中的响应脚本转换成 Codex 事件，供工作流测试复用。"""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
import inspect
from queue import Queue
import threading
from typing import Any
import uuid

from alphasolve.agent.codex_session import ToolRequest
from alphasolve.llm.types import Message, ToolDef


class ScriptedCodexSession:
    """只在测试中模拟模型端；真正的工具始终由 Agent 所在线程执行。"""

    def __init__(self, *, client: Any, system_prompt: str, tools: list[ToolDef]) -> None:
        self.client = client
        self.tools = tools
        self.messages = [Message(role="system", content=system_prompt)]
        self.events: Queue = Queue()
        self.thread_id = f"test-{uuid.uuid4().hex}"
        self._closed = threading.Event()
        self._pending: Future | None = None
        self._thread: threading.Thread | None = None
        self._turn = 0
        self._usage = {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0}

    def start_turn(self, task: str) -> None:
        self._turn += 1
        self.messages.append(Message(role="user", content=task))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        pending = self._pending
        if pending is not None:
            pending.cancel()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def _run(self) -> None:
        try:
            while not self._closed.is_set():
                kwargs = {"messages": list(self.messages), "tools": self.tools}
                parameters = inspect.signature(self.client.complete).parameters
                if "delta_sink" in parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
                ):
                    kwargs["delta_sink"] = self._on_delta
                response = self.client.complete(**kwargs)
                message = response.message
                self.messages.append(message)
                self._record_usage(response)
                if message.reasoning_content:
                    self.events.put(("item/completed", {"item": {
                        "type": "reasoning", "id": f"reasoning-{uuid.uuid4().hex}",
                        "summary": [message.reasoning_content], "content": [],
                    }}))
                if message.content:
                    self.events.put(("item/completed", {"item": {
                        "type": "agentMessage", "id": f"message-{uuid.uuid4().hex}",
                        "text": message.content,
                        "phase": "commentary" if message.tool_calls else "final_answer",
                    }}))
                if not message.tool_calls:
                    self.events.put(("turn/completed", {"turn": {
                        "id": f"turn-{self._turn}", "status": "completed",
                    }}))
                    return
                for call in message.tool_calls:
                    if self._closed.is_set():
                        return
                    pending: Future[dict] = Future()
                    self._pending = pending
                    self.events.put(ToolRequest(
                        params={"tool": call.name, "arguments": call.args, "callId": call.id},
                        response=pending,
                    ))
                    output = pending.result()
                    self._pending = None
                    self.messages.append(Message(
                        role="tool", tool_call_id=call.id, name=call.name,
                        content="\n".join(item.get("text", "") for item in output.get("contentItems", [])),
                    ))
        except CancelledError:
            if not self._closed.is_set():
                raise
        except BaseException as exc:
            if not self._closed.is_set():
                self.events.put(("_failure", {"error": exc}))

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        last = {
            "inputTokens": usage.input_tokens,
            "outputTokens": usage.output_tokens,
            "cachedInputTokens": usage.cached_tokens,
        }
        for key, value in last.items():
            self._usage[key] += value
        self.events.put(("thread/tokenUsage/updated", {
            "threadId": self.thread_id,
            "tokenUsage": {"total": dict(self._usage), "last": last},
        }))

    def _on_delta(self, delta: Any) -> None:
        if delta.type == "text":
            self.events.put(("item/agentMessage/delta", {"delta": delta.text}))
        elif delta.type == "reasoning":
            self.events.put(("item/reasoning/summaryTextDelta", {"delta": delta.text}))
