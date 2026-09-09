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
    Agent,
    AgentConfig,
    AgentEventSink,
    AgentRunError,
    AgentRunResult,
    AgentSuite,
    Workspace,
)
from alphasolve.agent.ui._render_shared import RICH_CONSOLE
from alphasolve.agent.ui.cli_app import make_repl_event_sink
from alphasolve.llm import CodexClient

from .cold_start import ColdStartRuntime
from .orchestrator import Orchestrator, WorkerManager
from .policy import SolverPolicy
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
        client_factory: Callable[[AgentConfig], CodexClient],
        console: Console = RICH_CONSOLE,
        policy: SolverPolicy | None = None,
        max_workers: int | None = None,
        max_verify_rounds: int | None = None,
        verifier_scaling_factor: int | None = None,
        subagent_max_depth: int | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.suite = suite
        self.client_factory = client_factory
        self.console = console
        base_policy = policy or SolverPolicy.from_settings(suite.settings)
        self.policy = base_policy.with_overrides(
            max_workers=max_workers,
            max_verify_rounds=max_verify_rounds,
            verifier_scaling_factor=verifier_scaling_factor,
            subagent_max_depth=subagent_max_depth,
        )
        self.max_workers = self.policy.max_workers
        self.max_verify_rounds = self.policy.max_verify_rounds
        self.verifier_scaling_factor = self.policy.verifier_scaling_factor
        self.subagent_max_depth = self.policy.subagent_max_depth
        self.stop_event = threading.Event()
        self.worker_stop_event = threading.Event()
        self._event_sink = make_repl_event_sink(console)
        self.layout = _snapshot_layout(self.project_dir)
        self._manager: WorkerManager | None = None
        self._cold_start_runtime: ColdStartRuntime | None = None
        self._orchestrator: Orchestrator | None = None
        self._agent: Agent | None = None

    def cancel(self) -> None:
        self.stop_event.set()
        self.worker_stop_event.set()

    def close(self) -> None:
        if self._agent is not None:
            self._agent.close()
            self._agent = None
        if self._manager is not None:
            graceful = self.stop_event.is_set() and self._manager.solved_result is None
            self._manager.close(graceful=graceful)
            self._manager = None
        self._cold_start_runtime = None
        self._orchestrator = None

    def run(self) -> None:
        """交互式运行真实 orchestrator agent。"""
        self.console.print("[bold cyan]AlphaSolve Orchestrator Agent[/bold cyan]")
        self.console.print(f"[dim]workspace snapshot:[/dim] {self.project_dir}")
        self.console.print("[dim]commands:[/dim] /exit, /quit, Ctrl+C")
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
                self.run_once(prompt)
            except AgentRunError as exc:
                if exc.fatal:
                    raise
                self.console.print(str(exc), style="red", markup=False)
                continue

    def run_once(
        self,
        prompt: str,
        *,
        event_sink: AgentEventSink | None = _sentinel,
    ) -> AgentRunResult:
        """用真实 orchestrator 配置执行一次；不会主动创建 snapshot 缺失目录。"""
        if event_sink is _sentinel:
            event_sink = self._event_sink
        agent = self.build_agent()
        agent.event_sink = event_sink
        try:
            return agent.run(prompt)
        except AgentRunError as exc:
            if exc.fatal:
                self.cancel()
            raise

    def tool_defs(self):
        """展示真实 orchestrator 工具；不启动冷启动任务或模型请求。"""
        agent = self.build_agent()
        return agent.tool_registry.tool_defs(
            agent.config.tools, agent.config.tool_parameters, agent.config.tool_descriptions,
        )

    def build_agent(self) -> Agent:
        """同一个交互入口复用同一个 Codex 会话。"""
        if self._agent is not None:
            return self._agent
        manager = self._manager_or_create()
        orchestrator = self._orchestrator_or_create()
        subagents = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=0,
            execution_gateway=None,
            session_prefix="orchestrator",
            allow_research_reviewer=True,
            file_access_factory=lambda: RoleWorkspaceAccess.orchestrator_subagent(
                Workspace(self.layout.workspace_dir)
            ),
            stop_event=self.stop_event,
        )
        subagents.reviewer_state_provider = orchestrator._reviewer_frontier_projection
        self._agent = orchestrator.build_agent(
            manager,
            subagents=subagents,
            event_sink=None,
        )
        return self._agent

    def _orchestrator_or_create(self) -> Orchestrator:
        if self._orchestrator is None:
            self._orchestrator = Orchestrator(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                policy=self.policy,
                renderer=None,
                execution_gateway=None,
                curator_queue=None,
                log_session=None,
                stop_event=self.stop_event,
                worker_stop_event=self.worker_stop_event,
                cold_start_runtime=self._cold_start_runtime_or_create(),
            )
        return self._orchestrator

    def _cold_start_runtime_or_create(self) -> ColdStartRuntime:
        if self._cold_start_runtime is None:
            self._cold_start_runtime = ColdStartRuntime(
                layout=self.layout,
                max_workers=self.policy.max_workers,
                threshold=self.policy.cold_start_verified_proposition_threshold,
                stop_event=self.stop_event,
            )
        return self._cold_start_runtime

    def _manager_or_create(self) -> WorkerManager:
        if self._manager is None:
            self._manager = WorkerManager(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                policy=self.policy,
                renderer=None,
                execution_gateway=None,
                curator_queue=None,
                log_session=None,
                stop_event=self.worker_stop_event,
                run_stop_event=self.stop_event,
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
