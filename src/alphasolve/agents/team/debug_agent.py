from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.text import Text

from alphasolve.agents.general import AgentRunError, AgentRunResult, AgentSuiteConfig, GeneralAgentConfig, GeneralPurposeAgent, Workspace
from alphasolve.llm.types import Message
from alphasolve.utils.rich_renderer import RICH_CONSOLE

from .tools import ClientFactory, RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry, register_agent_tool


DEBUG_AGENT_TOOLS = ["Glob", "Grep", "ListDir", "Read", "Write", "Edit"]
DEBUG_AGENT_EXTENSIONS = (
    "",
    ".bat",
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".lean",
    ".lock",
    ".md",
    ".py",
    ".ps1",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
)


@dataclass
class DebugToolEvent:
    name: str
    id: str = ""
    arguments: Any = None
    status: str = "running"
    result: str = ""


class GeneralAgentDebugRenderer:
    def __init__(
        self,
        *,
        console: Console = RICH_CONSOLE,
        workspace: Path,
        tools: list[str],
        screen: bool = False,
    ) -> None:
        del screen
        self.console = console
        self.workspace = workspace
        self.tools = tools
        self._turn = 0
        self._thinking = ""
        self._answer = ""
        self._tools: list[DebugToolEvent] = []
        self._active_tools: dict[str, DebugToolEvent] = {}
        self._printed_assistant = ""
        self._lock = threading.RLock()

    @property
    def tool_events(self) -> list[DebugToolEvent]:
        with self._lock:
            return list(self._tools)

    def start(self, prompt: str) -> None:
        with self._lock:
            self._turn = 0
            self._thinking = ""
            self._answer = ""
            self._tools = []
            self._active_tools = {}
            self._printed_assistant = ""
            self.console.print()
            self.console.print(_message_block("user", prompt, label_style="bold cyan", body_style="white on #1f2937"))

    def stop(self) -> None:
        pass

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        with self._lock:
            if event_type == "run_start":
                self.console.print(_status_line("agent_debug started", "dim"))
            elif event_type == "model_request":
                self._turn = int(event.get("turn") or self._turn)
                self.console.print(_status_line(f"thinking turn {self._turn}", "dim"))
            elif event_type == "thinking_delta":
                self._turn = int(event.get("turn") or self._turn)
                self._thinking = str(event.get("content") or "")
            elif event_type == "assistant_delta":
                self._turn = int(event.get("turn") or self._turn)
                self._answer = str(event.get("content") or "")
            elif event_type == "thinking":
                self._thinking = str(event.get("content") or self._thinking)
                if self._thinking.strip():
                    self.console.print(_message_block("thinking", self._thinking, label_style="italic magenta", body_style="italic #a78bfa"))
            elif event_type == "assistant_message":
                self._answer = str(event.get("content") or self._answer)
                if self._answer.strip():
                    self._printed_assistant = self._answer
                    self.console.print(_message_block("assistant", self._answer, label_style="bold green", body_style="white"))
            elif event_type == "tool_call":
                name = str(event.get("name") or "")
                args = event.get("arguments")
                if args is None:
                    args = event.get("raw_arguments") or ""
                tool = DebugToolEvent(
                    id=str(event.get("tool_call_id") or ""),
                    name=name,
                    arguments=args,
                    status="running",
                )
                self._tools.append(tool)
                if tool.id:
                    self._active_tools[tool.id] = tool
                self.console.print(_tool_call_block(tool))
            elif event_type == "tool_result":
                name = str(event.get("name") or "")
                result = str(event.get("content") or "")
                is_error = bool(event.get("is_error"))
                tool_id = str(event.get("tool_call_id") or "")
                tool = self._active_tools.pop(tool_id, None) if tool_id else None
                if tool is None:
                    for candidate in reversed(self._tools):
                        if candidate.name == name and candidate.status == "running":
                            tool = candidate
                            break
                if tool is None:
                    tool = DebugToolEvent(id=tool_id, name=name, status="running")
                    self._tools.append(tool)
                tool.status = "error" if is_error else "done"
                tool.result = result
                self.console.print(_tool_result_block(tool))
            elif event_type == "model_retry":
                self.console.print(_status_line(f"retry {event.get('attempt')}: {event.get('error_type')}", "yellow"))
            elif event_type == "run_finish":
                final_answer = str(event.get("final_answer") or "")
                if final_answer.strip() and final_answer != self._printed_assistant:
                    self.console.print(_message_block("assistant", final_answer, label_style="bold green", body_style="white"))
                self.console.print(_status_line("done", "green"))
            elif event_type == "run_stopped":
                self.console.print(_status_line(str(event.get("reason") or "stopped"), "yellow"))
            elif event_type == "run_error":
                self.console.print(_status_line(str(event.get("error") or "error"), "red"))


def _message_block(label: str, body: str, *, label_style: str, body_style: str) -> Text:
    text = Text()
    text.append(label + "\n", style=label_style)
    for line in _lines(body):
        text.append("  " + line + "\n", style=body_style)
    if not body.strip():
        text.append("  ...\n", style="dim")
    return text


def _status_line(message: str, style: str) -> Text:
    text = Text()
    text.append("  " + message, style=style)
    return text


def _tool_call_block(tool: DebugToolEvent) -> Text:
    text = Text()
    text.append(_tool_call_title(tool), style="bold yellow")
    text.append(" running", style="yellow")
    args = _tool_call_details(tool)
    if args:
        text.append("\n")
        for line in _lines(args):
            text.append("  " + line + "\n", style="dim")
    return text


def _tool_result_block(tool: DebugToolEvent) -> Text:
    text = Text()
    if tool.status == "error":
        text.append(_tool_call_title(tool) + " error", style="bold red")
        body_style = "red"
    else:
        text.append(_tool_call_title(tool) + " done", style="bold green")
        body_style = "#9ca3af"
    if tool.result:
        text.append("\n")
        for line in _lines(tool.result):
            text.append("  " + line + "\n", style=body_style)
    return text


def _tool_call_title(tool: DebugToolEvent) -> str:
    args = tool.arguments if isinstance(tool.arguments, dict) else {}
    name = tool.name
    if name == "Read":
        return f"read {_arg(args, 'path')}"
    if name == "Write":
        return f"write {_arg(args, 'path')}"
    if name == "Edit":
        return f"edit {_arg(args, 'path')}"
    if name == "Grep":
        return f"grep /{_arg(args, 'pattern')}/ in {_arg(args, 'path', '.')}"
    if name == "Glob":
        return f"glob {_arg(args, 'pattern')} in {_arg(args, 'path', '.')}"
    if name == "ListDir":
        return f"list {_arg(args, 'path', '.')}"
    if name == "Agent":
        return f"agent {_arg(args, 'type')} ({_arg(args, 'description', 'task')})"
    return name


def _tool_call_details(tool: DebugToolEvent) -> str:
    args = tool.arguments
    if not isinstance(args, dict):
        return _short_json(args)
    return json.dumps(args, ensure_ascii=False, indent=2)


def _arg(args: dict[str, Any], key: str, default: str = "...") -> str:
    value = args.get(key, default)
    if value is None or value == "":
        return default
    return _preview(str(value), limit=80)


class GeneralAgentDebugApp:
    def __init__(
        self,
        *,
        project_dir: str | Path,
        client_factory: ClientFactory,
        suite: AgentSuiteConfig | None = None,
        console: Console = RICH_CONSOLE,
        renderer_factory: Callable[..., GeneralAgentDebugRenderer | None] | None = GeneralAgentDebugRenderer,
        max_turns: int = 80,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.client_factory = client_factory
        self.suite = suite
        self.console = console
        self.max_turns = max_turns
        self.stop_event = threading.Event()
        self._renderer_factory = renderer_factory
        self._renderer: GeneralAgentDebugRenderer | None = None
        self._history: list[Message] = []
        self._agent_config: GeneralAgentConfig | None = None
        self._tools: list[str] = []

    def cancel(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        self.console.print("[bold cyan]AlphaSolve GeneralPurposeAgent debug mode[/bold cyan]")
        self.console.print(f"[dim]workspace:[/dim] {self.project_dir}")
        self.console.print("[dim]commands:[/dim] /exit, /quit, Ctrl+C")
        while not self.stop_event.is_set():
            try:
                prompt = input("\nagent_debug> ").strip()
            except EOFError:
                break
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                break
            try:
                self.run_once(prompt)
            except AgentRunError as exc:
                self.console.print(f"[red]{exc}[/red]")
                continue

    def run_once(self, prompt: str) -> AgentRunResult:
        config = self.build_config()
        registry = self.build_registry()
        renderer = self._make_renderer()
        self._renderer = renderer
        if renderer is not None:
            renderer.start(prompt)
        try:
            agent = GeneralPurposeAgent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=renderer.handle_event if renderer is not None else _silent_event_sink,
                stop_event=self.stop_event,
            )
            result = agent.run(prompt, extra_messages=self._history)
            self._history = [message for message in result.messages if message.role != "system"]
            return result
        finally:
            if renderer is not None:
                renderer.stop()
            self._renderer = None

    def build_config(self) -> GeneralAgentConfig:
        if self.suite is not None and "agent_debug" in self.suite.agents:
            config = self.suite.agents["agent_debug"]
            self._agent_config = config
            self._tools = list(config.tools)
            return config
        self._agent_config = GeneralAgentConfig(
            name="agent_debug",
            system_prompt="",
            tools=tuple(DEBUG_AGENT_TOOLS),
            max_turns=self.max_turns,
        )
        self._tools = list(self._agent_config.tools)
        return self._agent_config

    def build_registry(self):
        access = RoleWorkspaceAccess(
            workspace=Workspace(self.project_dir),
            read_root_rel=".",
            write_root_rel=".",
            allowed_extensions=DEBUG_AGENT_EXTENSIONS,
        )
        registry = build_workspace_tool_registry(access, allow_write=True)
        if self.suite is not None and self.suite.subagents and self._agent_config is not None and "Agent" in self._agent_config.tools:
            subagent_service = SubagentService(
                suite=self.suite,
                client_factory=self.client_factory,
                max_depth=2,
                session_prefix="agent_debug",
                stop_event=self.stop_event,
            )
            register_agent_tool(
                registry,
                agent_config=self._agent_config,
                subagent_service=subagent_service,
                depth=0,
            )
        return registry

    def _make_renderer(self) -> GeneralAgentDebugRenderer | None:
        if self._renderer_factory is None:
            return None
        return self._renderer_factory(
            console=self.console,
            workspace=self.project_dir,
            tools=list(self._tools) if self._tools else list(DEBUG_AGENT_TOOLS),
            screen=False,
        )


def _short_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def _preview(value: str, *, limit: int) -> str:
    text = " ".join(str(value).replace("\r\n", "\n").replace("\r", "\n").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _lines(value: str) -> list[str]:
    return str(value).replace("\r\n", "\n").replace("\r", "\n").splitlines() or [str(value)]


def _silent_event_sink(_event: dict[str, Any]) -> None:
    pass
