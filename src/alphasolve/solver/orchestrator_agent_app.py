"""真实 orchestrator 的单 agent 测试入口。

这个模块服务于 ``alphasolve --agent --profile orchestrator``：复用 solver
实际运行时的 orchestrator 系统提示词、工具白名单和工具装配，但把 user message
交给交互输入或 ``-p`` 参数。它刻意留在 solver 层，避免第二层 agent 反向依赖
AlphaSolve 的研究工作区概念。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from rich.console import Console

from alphasolve.agent import (
    AgentConfig,
    AgentContextPolicy,
    AgentEventSink,
    AgentRunError,
    AgentRunResult,
    AgentSuite,
    Workspace,
)
from alphasolve.agent.ui._render_shared import RICH_CONSOLE
from alphasolve.agent.ui.cli_app import make_repl_event_sink
from alphasolve.llm.types import ChatClient

from .orchestrator import Orchestrator, WorkerManager
from .project import ProjectLayout
from .subagent_service import SubagentService
from .workspace_access import RoleWorkspaceAccess

_sentinel = object()


class OrchestratorAgentApp:
    """把当前目录当作 AlphaSolve workspace snapshot 运行真实 orchestrator agent。"""

    def __init__(
        self,
        *,
        project_dir: str | Path,
        suite: AgentSuite,
        client_factory: Callable[[AgentConfig], ChatClient],
        console: Console = RICH_CONSOLE,
        max_workers: int = 4,
        max_verify_rounds: int = 2,
        verifier_scaling_factor: int = 1,
        subagent_max_depth: int = 0,
        context_policy: AgentContextPolicy | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.suite = suite
        self.client_factory = client_factory
        self.console = console
        self.max_workers = max(1, int(max_workers))
        self.max_verify_rounds = max(1, int(max_verify_rounds))
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = max(0, int(subagent_max_depth))
        self.context_policy = context_policy
        self.stop_event = threading.Event()
        self.worker_stop_event = threading.Event()
        self._event_sink = make_repl_event_sink(console)
        self.layout = _snapshot_layout(self.project_dir)
        self._manager: WorkerManager | None = None

    def cancel(self) -> None:
        self.stop_event.set()
        self.worker_stop_event.set()

    def close(self) -> None:
        if self._manager is not None:
            graceful = self.stop_event.is_set() and self._manager.solved_result is None
            self._manager.close(graceful=graceful)
            self._manager = None

    def run(self) -> None:
        """交互式运行真实 orchestrator agent。"""
        self.console.print("[bold cyan]AlphaSolve Orchestrator Agent[/bold cyan]")
        self.console.print(f"[dim]workspace snapshot:[/dim] {self.project_dir}")
        self.console.print("[dim]commands:[/dim] /exit, /quit, Ctrl+C")
        history = []
        while not self.stop_event.is_set():
            try:
                prompt = input("\norchestrator> ").strip()
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
        self,
        prompt: str,
        *,
        extra_messages=None,
        event_sink: AgentEventSink | None = _sentinel,
    ) -> AgentRunResult:
        """用真实 orchestrator 配置执行一次；不会主动创建 snapshot 缺失目录。"""
        if event_sink is _sentinel:
            event_sink = self._event_sink
        manager = self._manager_or_create()
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=0,
            execution_gateway=None,
            session_prefix="orchestrator",
            file_access_factory=lambda: RoleWorkspaceAccess.orchestrator_subagent(
                Workspace(self.layout.workspace_dir)
            ),
            stop_event=self.stop_event,
        )
        orchestrator = Orchestrator(
            layout=self.layout,
            suite=self.suite,
            client_factory=self.client_factory,
            max_workers=self.max_workers,
            max_verify_rounds=self.max_verify_rounds,
            verifier_scaling_factor=self.verifier_scaling_factor,
            subagent_max_depth=self.subagent_max_depth,
            renderer=None,
            execution_gateway=None,
            curator_queue=None,
            log_session=None,
            stop_event=self.stop_event,
            worker_stop_event=self.worker_stop_event,
        )
        agent = orchestrator.build_agent(
            manager,
            subagents=subagents,
            event_sink=event_sink,
            context_policy=self.context_policy,
        )
        return agent.run(prompt, extra_messages=extra_messages or [])

    def _manager_or_create(self) -> WorkerManager:
        if self._manager is None:
            self._manager = WorkerManager(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                max_workers=self.max_workers,
                max_verify_rounds=self.max_verify_rounds,
                verifier_scaling_factor=self.verifier_scaling_factor,
                subagent_max_depth=self.subagent_max_depth,
                renderer=None,
                execution_gateway=None,
                curator_queue=None,
                log_session=None,
                stop_event=self.worker_stop_event,
            )
        return self._manager


def _snapshot_layout(root: Path) -> ProjectLayout:
    """把任意目录解释为 AlphaSolve workspace snapshot。"""
    hint_path = root / "hint.md"
    return ProjectLayout(
        project_root=root,
        problem_path=root / "problem.md",
        hint_path=hint_path if hint_path.is_file() else None,
        workspace_dir=root,
        logs_dir=root / "logs",
        knowledge_dir=root / "knowledge",
        unverified_dir=root / "unverified_propositions",
        verified_dir=root / "verified_propositions",
    )
