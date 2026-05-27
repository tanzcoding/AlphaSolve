"""第二层 Agent 的 CLI REPL —— 由 `alphasolve --agent` 启动。

设计原则（spec §2）：
- 完全独立于 solver/：不能 import alphasolve.solver.* 或 alphasolve.workflow.*
- 工具集 = build_default_tool_registry 全集，加一个第二层 scoped_explorer Agent 工具
- 系统提示词暂为空（phase D 加可配置机制）
- 主 agent REPL；子 agent 只做限定范围探索，不承担第三层编排
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from alphasolve.agent import (
    Agent,
    AgentConfig,
    AgentEventSink,
    AgentRunError,
    AgentRunResult,
    Workspace,
    build_default_tool_registry,
    register_agent_tool,
)
from alphasolve.agent.shell import has_bash
from alphasolve.agent.ui._render_shared import RICH_CONSOLE
from alphasolve.llm.types import ChatClient


# 第二层 default registry 提供的全部基础工具。具体可用清单由
# build_default_tool_registry 决定；这里硬编码白名单，避免把需要第三层专门
# 注册的研究/编排工具误开。Bash vs Shell 由平台决定（has_bash 探测一次）。
_sentinel = object()

_BASE_AGENT_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "MakeDir",
    "Rename",
    "Move",
    "Delete",
    "Glob",
    "ListDir",
    "Grep",
    "GetCurrentTime",
)
_SCOPED_EXPLORER_TYPE = "scoped_explorer"


def _default_agent_tools(*, include_agent: bool = True) -> tuple[str, ...]:
    shell_tool = "Bash" if has_bash() else "Shell"
    tools = (*_BASE_AGENT_TOOLS, shell_tool)
    if include_agent:
        return (*tools, "Agent")
    return tools


class _CliSubagentDispatcher:
    """`--agent` 入口使用的第二层通用子 agent 调度器。"""

    def __init__(
        self,
        *,
        project_dir: Path,
        parent_config: AgentConfig,
        client_factory: Callable[[AgentConfig], ChatClient],
        stop_event: threading.Event,
    ) -> None:
        self.project_dir = project_dir
        self.parent_config = parent_config
        self.client_factory = client_factory
        self.stop_event = stop_event

    def available_types(self) -> list[str]:
        return [_SCOPED_EXPLORER_TYPE]

    def describe_type(self, agent_type: str) -> str:
        if agent_type == _SCOPED_EXPLORER_TYPE:
            return (
                "Use for scoped workspace exploration. Give it one directory, file group, "
                "or narrow question; it returns evidence and local status, not the global decision."
            )
        return "Unknown subagent type."

    def call(self, agent_type: str, description: str, prompt: str, *, depth: int = 0) -> str:
        if agent_type != _SCOPED_EXPLORER_TYPE:
            raise ValueError(f"unknown agent type: {agent_type}")
        if depth >= 1:
            raise ValueError("scoped_explorer does not launch nested subagents")

        subagent_prompt = (
            self.parent_config.system_prompt.rstrip()
            + "\n\n# Scoped Explorer Mode\n\n"
            "You are a scoped exploration subagent. Stay within the area or question named by the prompt unless a directly cited file requires a small cross-reference. "
            "Return a concise evidence report: files inspected, important findings with paths or line references when available, unresolved issues, and the next local step. "
            "Do not make the final workspace-wide decision unless the prompt explicitly asks for it; leave global comparison to the caller.\n"
        )
        config = AgentConfig(
            name=f"agent:{agent_type}",
            tier=self.parent_config.tier,
            system_prompt=subagent_prompt,
            tools=_default_agent_tools(include_agent=False),
            tool_parameters=self.parent_config.tool_parameters,
            tool_descriptions=self.parent_config.tool_descriptions,
            max_turns=min(self.parent_config.max_turns, 30),
            skills=self.parent_config.skills,
            when_to_use=self.describe_type(agent_type),
            metadata={"parent_agent": self.parent_config.name, "description": description},
        )
        registry = build_default_tool_registry(Workspace(self.project_dir))
        agent = Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            stop_event=self.stop_event,
            caller_context={
                "parent_agent_id": self.parent_config.name,
                "depth": depth + 1,
                "agent_type": agent_type,
                "description": description,
            },
        )
        result = agent.run(prompt, description=description)
        return result.final_answer


def make_repl_event_sink(console: Console) -> AgentEventSink:
    """Build an event_sink that renders intermediate agent events to the console.

    每个 turn 的事件顺序（streaming reasoning model）：
      thinking_delta* → assistant_delta* → thinking → assistant_message
    非 streaming provider 没有 delta 事件，直接 thinking → assistant_message。

    避免重复打印：
    - delta 流式输出后 assistant_message 不再重复打印正文
    - run_finish 的 final_answer 总是已经展示过，无需再印
    """
    # Mutable state shared across events within one turn; reset at assistant_message.
    state: dict[str, bool] = {"had_reasoning": False, "had_text": False}

    def sink(event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype == "thinking_delta":
            delta = event.get("delta", "")
            if delta:
                console.print(delta, end="", style="bright_blue dim")
                state["had_reasoning"] = True
        elif etype == "thinking":
            reasoning = event.get("content", "")
            streamed = event.get("streamed", False)
            if reasoning and not streamed:
                # Non-streaming provider: print full block.
                console.print(f"[dim bright_blue]{reasoning}[/dim bright_blue]")
                state["had_reasoning"] = True
        elif etype == "assistant_delta":
            delta = event.get("delta", "")
            if delta:
                if state["had_reasoning"] and not state["had_text"]:
                    console.print()  # reasoning → text transition
                console.print(delta, end="")
                state["had_text"] = True
        elif etype == "assistant_message":
            content = event.get("content", "")
            streamed = event.get("streamed_content", False)
            if content and not streamed:
                console.print(content)
            elif state["had_text"]:
                console.print()  # end delta-streamed line
            tc_count = event.get("tool_call_count", 0)
            if tc_count:
                console.print(f"[dim](calling {tc_count} tool(s))[/dim]")
            state["had_reasoning"] = False
            state["had_text"] = False
        elif etype == "tool_call":
            name = event.get("name", "")
            args = event.get("arguments", {})
            console.rule(f"[bold green]{name}[/bold green]")
            for k, v in args.items():
                console.print(f"  [dim]{k}[/dim] = {v}")
        elif etype == "tool_result":
            content = event.get("content", "")
            is_error = event.get("is_error", False)
            tag = "[red]✗ Error[/red]" if is_error else "[green]✓ Result[/green]"
            console.print(tag)
            console.print(content)
        elif etype == "run_finish":
            pass  # content already visible via deltas or assistant_message
        elif etype == "run_error":
            error = event.get("error", "")
            console.print(f"[red]Error: {error}[/red]")
        elif etype == "run_stopped":
            console.print("[dim]Agent stopped.[/dim]")
    return sink


def make_print_debug_event_sink(console: Console) -> AgentEventSink:
    """Build an event sink for `alphasolve --agent -p --debug`.

    It is intentionally narrower than the interactive REPL renderer: print mode
    still prints the final answer exactly once after the run, while this sink
    shows the intermediate reasoning and tool traffic needed for debugging.
    """
    state: dict[str, bool] = {"in_reasoning_stream": False}

    def _plain(text: Any, *, style: str | None = None) -> None:
        console.print(str(text), style=style, markup=False)

    def sink(event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype == "run_start":
            console.rule("[bold cyan]agent debug[/bold cyan]")
            _plain(f"agent: {event.get('agent', '')}", style="dim")
            enabled = ", ".join(event.get("enabled_tools", []) or [])
            if enabled:
                _plain(f"tools: {enabled}", style="dim")
        elif etype == "model_request":
            console.rule(f"[bold blue]turn {event.get('turn', '?')}[/bold blue]")
            state["in_reasoning_stream"] = False
        elif etype == "thinking_delta":
            delta = event.get("delta", "")
            if delta:
                if not state["in_reasoning_stream"]:
                    console.print("[bold bright_blue]COT[/bold bright_blue]")
                    state["in_reasoning_stream"] = True
                console.print(delta, end="", style="bright_blue dim", markup=False)
        elif etype == "thinking":
            reasoning = event.get("content", "")
            streamed = event.get("streamed", False)
            if reasoning and not streamed:
                console.print("[bold bright_blue]COT[/bold bright_blue]")
                _plain(reasoning, style="bright_blue dim")
            if state["in_reasoning_stream"]:
                console.print()
                state["in_reasoning_stream"] = False
        elif etype == "assistant_message":
            content = event.get("content", "")
            tool_call_count = int(event.get("tool_call_count") or 0)
            if content and tool_call_count:
                console.print("[bold]assistant before tool call[/bold]")
                _plain(content)
            if tool_call_count:
                _plain(f"calling {tool_call_count} tool(s)", style="dim")
        elif etype == "tool_call":
            name = event.get("name", "")
            console.rule(f"[bold green]tool call: {name}[/bold green]")
            args = event.get("arguments", {}) or {}
            for key, value in args.items():
                _plain(f"{key}: {value}")
        elif etype == "tool_result":
            name = event.get("name", "")
            is_error = bool(event.get("is_error"))
            label = "error" if is_error else "result"
            style = "red" if is_error else "green"
            console.rule(f"[bold {style}]tool {label}: {name}[/bold {style}]")
            _plain(event.get("content", ""))
        elif etype == "run_error":
            console.rule("[bold red]agent error[/bold red]")
            _plain(event.get("error", ""), style="red")
        elif etype == "run_stopped":
            console.rule("[bold yellow]agent stopped[/bold yellow]")
            _plain(event.get("reason", ""), style="yellow")

    return sink


class AgentApp:
    """`alphasolve --agent` CLI 入口。

    跟 solver 域的 SubagentService 完全解耦，纯第二层 agent + 默认工具集。
    """

    def __init__(
        self,
        *,
        project_dir: str | Path,
        client_factory: Callable[[AgentConfig], ChatClient],
        console: Console = RICH_CONSOLE,
        max_turns: int = 80,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.client_factory = client_factory
        self.console = console
        self.max_turns = max_turns
        self.stop_event = threading.Event()
        self._event_sink = make_repl_event_sink(console)

    def cancel(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        """交互式 REPL。"""
        self.console.print("[bold cyan]AlphaSolve Agent[/bold cyan]")
        self.console.print(f"[dim]workspace:[/dim] {self.project_dir}")
        self.console.print("[dim]commands:[/dim] /exit, /quit, Ctrl+C")
        history = []
        while not self.stop_event.is_set():
            try:
                prompt = input("\nagent> ").strip()
            except EOFError:
                break
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                break
            try:
                result = self.run_once(prompt, extra_messages=history)
            except AgentRunError as exc:
                self.console.print(f"[red]{exc}[/red]")
                continue
            history = [m for m in result.messages if m.role != "system"]

    def run_once(
        self, prompt: str, *, extra_messages=None, event_sink: AgentEventSink | None = _sentinel,
    ) -> AgentRunResult:
        """单次执行。

        event_sink 默认用交互式 REPL sink；传入 None 可抑制所有中间输出
        （`alphasolve --agent -p PROMPT` 走此路径）。
        """
        config = self._build_config()
        registry = build_default_tool_registry(Workspace(self.project_dir))
        register_agent_tool(
            registry,
            agent_config=config,
            dispatcher=_CliSubagentDispatcher(
                project_dir=self.project_dir,
                parent_config=config,
                client_factory=self.client_factory,
                stop_event=self.stop_event,
            ),
        )
        if event_sink is _sentinel:
            event_sink = self._event_sink
        agent = Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            stop_event=self.stop_event,
            event_sink=event_sink,
        )
        return agent.run(prompt, extra_messages=extra_messages or [])

    def _build_config(self) -> AgentConfig:
        prompt_path = Path(__file__).resolve().parent.parent / "default_prompt.md"
        prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        return AgentConfig(
            name="agent",
            tier="balanced",
            system_prompt=prompt,
            tools=_default_agent_tools(),
            max_turns=self.max_turns,
        )
