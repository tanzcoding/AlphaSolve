"""第二层 Agent 的 CLI REPL —— 由 `alphasolve --agent` 启动。

设计原则（spec §2）：
- 完全独立于 solver/：不能 import alphasolve.solver.* 或 alphasolve.workflow.*
- 工具集 = build_default_tool_registry 全集（无 SubagentService、无 Agent 工具）
- 系统提示词暂为空（phase D 加可配置机制）
- 单 agent REPL，无多 agent 编排
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from rich.console import Console

from alphasolve.agent import (
    Agent,
    AgentConfig,
    AgentRunError,
    AgentRunResult,
    Workspace,
    build_default_tool_registry,
)
from alphasolve.agent.shell import has_bash
from alphasolve.agent.ui._render_shared import RICH_CONSOLE
from alphasolve.llm.types import ChatClient


# 第二层 default registry 提供的全部基础工具。具体可用清单由
# build_default_tool_registry 决定；这里硬编码白名单，避免把 "Agent"
# （需要 SubagentDispatcher）和需要专门注册的 SpawnWorker/TaskOutput
# 误开。Bash vs Shell 由平台决定（has_bash 探测一次）。
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


def _default_agent_tools() -> tuple[str, ...]:
    shell_tool = "Bash" if has_bash() else "Shell"
    return (*_BASE_AGENT_TOOLS, shell_tool)


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

    def run_once(self, prompt: str, *, extra_messages=None) -> AgentRunResult:
        """单次执行 —— 用于 `alphasolve --agent -p PROMPT`。"""
        config = self._build_config()
        registry = build_default_tool_registry(Workspace(self.project_dir))
        agent = Agent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
            stop_event=self.stop_event,
        )
        return agent.run(prompt, extra_messages=extra_messages or [])

    def _build_config(self) -> AgentConfig:
        return AgentConfig(
            name="agent",
            system_prompt="",  # phase D 加可配置默认
            tools=_default_agent_tools(),
            max_turns=self.max_turns,
        )
