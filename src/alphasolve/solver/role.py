"""Solver 第三层：Role —— 一个 worker 角色的 Agent 装配 + 运行的封装。

把原本散落在 worker.py 各 ``_run_*`` 方法里的 "7 步装配配方"（pull config → build
access policy → build SubagentService → build registry → register_agent_tool →
Agent → agent.run + 写 trace）集中到此处。调用方只看 ``Role.for_<name>(ctx,
...).run(task)``。

设计原则：
- ``RoleContext`` 只承载 worker-stable 资源；每次调用的 runtime data
  （workflow_index / attempt_index / config_name / proposition_rel 等）
  作为 factory 的关键字参数显式传入，便于阅读。
- ``Role.run`` 只做 "跑 agent + 写一条 base trace 条目"；任何附加 post-processing
  （比如 verifier_attempt 的 curator 提交、verdict_judge 的 verdict 解析）
  仍由调用方在 ``Role.run`` 返回的 ``AgentRunResult`` 上完成。
- Codex 适配器与工具公共 API（Agent / ToolRegistry / SubagentDispatcher）
  通过参数注入；本模块不暴露这些类型给上游调用方。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import (
    Agent,
    AgentConfig,
    AgentEventSink,
    AgentRunResult,
    AgentSuite,
    Workspace,
)

from .client_factory import ClientFactory
from .subagent_service import SubagentService
from .tool_runtime import build_solver_tool_registry
from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from alphasolve.solver.execution import ExecutionGateway
    from alphasolve.solver.logging.log_session import LogSession
    from .curator import CuratorQueue


@dataclass
class RoleContext:
    """A Worker's worker-stable resources, passed to ``Role.for_<role>`` factories.

    Per-role runtime data is NOT here—it's a kwarg of the factory. This keeps
    the context immutable across a worker's lifetime and makes call sites
    self-documenting.
    """
    workspace: Workspace
    worker_rel: str
    worker_dir: Path
    suite: AgentSuite
    client_factory: ClientFactory
    subagent_max_depth: int
    execution_gateway: "ExecutionGateway | None"
    curator_queue: "CuratorQueue | None"
    log_session: "LogSession | None"
    stop_event: threading.Event | None
    event_sink_factory: Callable[[str], AgentEventSink | None]
    trace: list[dict[str, Any]]


class Role:
    """An assembled-but-not-yet-run Agent for a solver role.

    Use one of the ``for_<role>`` classmethods to build, then call ``run(task)``.
    """

    def __init__(
        self,
        *,
        name: str,
        agent: Agent,
        trace_sink: list[dict[str, Any]],
        trace_extras: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._agent = agent
        self._trace_sink = trace_sink
        self._trace_extras = dict(trace_extras or {})

    @property
    def agent(self) -> Agent:
        """Underlying agent—exposed for tests and rare post-processing needs."""
        return self._agent

    def run(self, task: str, *, description: str = "") -> AgentRunResult:
        """Run the agent and append a base trace entry. Caller may inspect / post-process the result."""
        try:
            result = self._agent.run(task, description=description)
        finally:
            self._agent.close()
        self._trace_sink.append({
            "role": self.name,
            **self._trace_extras,
            "trace": result.trace,
            "final_answer": result.final_answer,
        })
        return result

    # ------------------------------------------------------------------
    # 角色工厂：每个 worker 角色一个 classmethod，封装完整装配配方。
    # ------------------------------------------------------------------
    @classmethod
    def for_generator(
        cls,
        ctx: RoleContext,
        *,
        curator_context_provider: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        event_sink_decorator: Callable[[AgentEventSink | None], AgentEventSink | None] | None = None,
    ) -> "Role":
        config = ctx.suite.agents["generator"]
        access = RoleWorkspaceAccess.generator(ctx.workspace, ctx.worker_rel)
        subagents = SubagentService(
            suite=ctx.suite,
            client_factory=ctx.client_factory,
            max_depth=ctx.subagent_max_depth,
            execution_gateway=ctx.execution_gateway,
            session_prefix=f"{ctx.worker_dir.name}/generator",
            curator_queue=ctx.curator_queue,
            curator_context_provider=curator_context_provider,
            log_session=ctx.log_session,
            stop_event=ctx.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess.worker_read_only(
                ctx.workspace, ctx.worker_rel
            ),
        )
        agent = _assemble_agent(
            config=config, access=access, subagents=subagents,
            ctx=ctx, event_sink_label="generator",
            event_sink_decorator=event_sink_decorator,
        )
        return cls(name="generator", agent=agent, trace_sink=ctx.trace)

    @classmethod
    def for_verifier_attempt(
        cls,
        ctx: RoleContext,
        *,
        config: AgentConfig,
        config_name: str,
        workflow_index: int,
        attempt_index: int,
    ) -> "Role":
        all_verifier_ws_rel = (ctx.worker_dir / "verifier_workspace").relative_to(
            ctx.workspace.root
        ).as_posix()
        access = RoleWorkspaceAccess.verifier_attempt(
            ctx.workspace, ctx.worker_rel,
            all_verifier_ws_rel=all_verifier_ws_rel,
            config_name=config_name,
        )
        subagents = SubagentService(
            suite=ctx.suite,
            client_factory=ctx.client_factory,
            max_depth=ctx.subagent_max_depth,
            execution_gateway=ctx.execution_gateway,
            session_prefix=f"{ctx.worker_dir.name}/verifier-workflow-{workflow_index}-attempt-{attempt_index}-{config_name}",
            log_session=ctx.log_session,
            stop_event=ctx.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess.verifier_attempt(
                ctx.workspace, ctx.worker_rel,
                all_verifier_ws_rel=all_verifier_ws_rel,
                config_name=config_name,
            ),
            curator_queue=ctx.curator_queue,
        )
        event_label = f"verifier_attempt w{workflow_index}.{attempt_index}"
        agent = _assemble_agent(
            config=config, access=access, subagents=subagents,
            ctx=ctx, event_sink_label=event_label,
        )
        return cls(
            name="verifier_attempt",
            agent=agent,
            trace_sink=ctx.trace,
            trace_extras={
                "workflow": workflow_index,
                "attempt": attempt_index,
                "config": config_name,
            },
        )

    @classmethod
    def for_theorem_checker(
        cls,
        ctx: RoleContext,
        *,
        attempt_index: int,
    ) -> "Role":
        config = ctx.suite.agents["theorem_checker"]
        access = RoleWorkspaceAccess.theorem_checker(ctx.workspace, ctx.worker_rel)
        subagents = SubagentService(
            suite=ctx.suite,
            client_factory=ctx.client_factory,
            max_depth=ctx.subagent_max_depth,
            execution_gateway=ctx.execution_gateway,
            session_prefix=f"{ctx.worker_dir.name}/theorem-checker",
            curator_queue=ctx.curator_queue,
            log_session=ctx.log_session,
            stop_event=ctx.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess.theorem_checker(
                ctx.workspace, ctx.worker_rel
            ),
        )
        agent = _assemble_agent(
            config=config, access=access, subagents=subagents,
            ctx=ctx, event_sink_label="theorem_checker",
        )
        return cls(
            name="theorem_checker",
            agent=agent,
            trace_sink=ctx.trace,
            trace_extras={"attempt": attempt_index},
        )

    @classmethod
    def for_reviser(
        cls,
        ctx: RoleContext,
        *,
        proposition_rel: str,
        workflow_index: int,
    ) -> "Role":
        config = ctx.suite.agents["reviser"]
        access = RoleWorkspaceAccess.reviser(
            ctx.workspace, ctx.worker_rel, proposition_rel=proposition_rel
        )
        subagents = SubagentService(
            suite=ctx.suite,
            client_factory=ctx.client_factory,
            max_depth=ctx.subagent_max_depth,
            execution_gateway=ctx.execution_gateway,
            session_prefix=f"{ctx.worker_dir.name}/reviser-workflow-{workflow_index}",
            curator_queue=ctx.curator_queue,
            log_session=ctx.log_session,
            stop_event=ctx.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess.worker_read_only(
                ctx.workspace, ctx.worker_rel
            ),
        )
        agent = _assemble_agent(
            config=config, access=access, subagents=subagents,
            ctx=ctx, event_sink_label=f"reviser w{workflow_index}",
        )
        return cls(
            name="reviser",
            agent=agent,
            trace_sink=ctx.trace,
            trace_extras={"workflow": workflow_index},
        )


def _assemble_agent(
    *,
    config: AgentConfig,
    access: RoleWorkspaceAccess,
    subagents: SubagentService,
    ctx: RoleContext,
    event_sink_label: str,
    event_sink_decorator: Callable[[AgentEventSink | None], AgentEventSink | None] | None = None,
) -> Agent:
    """Wire up the 5 standard pieces: registry → Agent tool → Agent."""
    registry = build_solver_tool_registry(access, agent_config=config, dispatcher=subagents)
    event_sink = ctx.event_sink_factory(event_sink_label)
    if event_sink_decorator is not None:
        event_sink = event_sink_decorator(event_sink)
    subagents.event_sink = event_sink
    return Agent(
        config=config,
        client=ctx.client_factory(config),
        tool_registry=registry,
        event_sink=event_sink,
        stop_event=ctx.stop_event,
    )
