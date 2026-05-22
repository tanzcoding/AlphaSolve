"""第三层子 agent 调度：递归创建子 agent、注入 RunPython/RunWolfram 等执行工具。"""
from __future__ import annotations

import json
import threading
import uuid
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import (
    AgentRunResult,
    GeneralAgentConfig,
    GeneralPurposeAgent,
    ToolRegistry,
    ToolResult,
)
from alphasolve.agent.tool_registry import build_default_tool_registry, register_agent_tool
from alphasolve.execution.runners import run_python, run_wolfram

from .client_factory import ClientFactory
from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway


def _last_plain_assistant_content(result: AgentRunResult) -> str:
    for message in reversed(result.messages):
        if message.role == "assistant" and not message.tool_calls:
            return message.content
    return result.final_answer


def _format_subagent_result(*, agent_type: str, session_id: str, text: str) -> str:
    return (
        f"agent_id: {session_id}\n"
        f"actual_subagent_type: {agent_type}\n"
        f"status: completed\n"
        f"\n"
        f"[summary]\n"
        f"{text}"
    )


class SubagentService:
    def __init__(
        self,
        *,
        suite,
        client_factory: ClientFactory,
        max_depth: int = 2,
        file_access_factory: Callable[[], RoleWorkspaceAccess] | None = None,
        file_allow_write: bool = False,
        execution_gateway: "ExecutionGateway | None" = None,
        session_prefix: str = "subagent",
        curator_queue: "Any | None" = None,
        curator_context_provider: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        log_session: "Any | None" = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.suite = suite
        self.client_factory = client_factory
        self.max_depth = max(0, int(max_depth))
        self.file_access_factory = file_access_factory
        self.file_allow_write = bool(file_allow_write)
        self.execution_gateway = execution_gateway
        self.session_prefix = session_prefix
        self.curator_queue = curator_queue
        self.curator_context_provider = curator_context_provider
        self.log_session = log_session
        self.stop_event = stop_event

    def available_types(self) -> list[str]:
        return sorted(self.suite.subagents)

    def describe_type(self, agent_type: str) -> str:
        """供第二层 register_agent_tool 用：返回 subagent 的 when_to_use 描述。

        与原 register_agent_tool 内部硬编码的查询逻辑一致：从 suite.subagents 拿
        config，返回 when_to_use（缺失时返回 agent_type 本身）。
        """
        config = self.suite.subagents.get(agent_type)
        if config is None or not config.when_to_use:
            return agent_type
        return config.when_to_use

    def call_tool(self, args: dict[str, Any], *, depth: int = 0) -> ToolResult:
        agent_type = str(args.get("type") or "")
        description = str(args.get("description") or "")
        prompt = str(args.get("prompt") or "")
        if not prompt.strip():
            return ToolResult("ERROR: prompt must not be empty", is_error=True)
        try:
            return ToolResult(self.call(agent_type, description, prompt, depth=depth))
        except Exception as exc:
            return ToolResult(f"ERROR: {exc}", is_error=True)

    def call(self, agent_type: str, description: str, prompt: str, *, depth: int = 0) -> str:
        session_id, result = self._run(agent_type, description, prompt, depth=depth)
        self._submit_curator_trace(
            agent_type=agent_type,
            session_id=session_id,
            description=description,
            prompt=prompt,
            result=result,
        )
        return _format_subagent_result(
            agent_type=agent_type,
            session_id=session_id,
            text=_last_plain_assistant_content(result),
        )

    def _submit_curator_trace(
        self,
        *,
        agent_type: str,
        session_id: str,
        description: str,
        prompt: str,
        result: AgentRunResult,
    ) -> None:
        if self.curator_queue is not None and not self.session_prefix.startswith("curator") and not self.session_prefix.startswith("orchestrator"):
            from .curator import CuratorTask
            caller_context = None
            if self.curator_context_provider is not None:
                try:
                    caller_context = self.curator_context_provider(
                        {
                            "agent_type": agent_type,
                            "session_id": session_id,
                            "description": description,
                            "prompt": prompt,
                            "final_answer": result.final_answer,
                            "turns": result.turns,
                        }
                    )
                except Exception as exc:
                    caller_context = {"curator_context_error": str(exc)}
            self.curator_queue.submit(CuratorTask(
                trace_segment=result.trace,
                source_label=f"{self.session_prefix}/{agent_type}",
                caller_context=caller_context,
            ))

    def _run(self, agent_type: str, description: str, prompt: str, *, depth: int = 0) -> tuple[str, AgentRunResult]:
        config = self.suite.subagents.get(agent_type)
        if config is None:
            allowed = ", ".join(self.available_types())
            raise ValueError(f"unknown subagent type: {agent_type}. Allowed types: {allowed}")
        session_id = self._make_session_id(agent_type=agent_type, depth=depth)
        registry = self._build_subagent_registry(depth=depth, session_id=session_id, config=config)
        enabled_tools = list(config.tools)
        if self.file_access_factory is not None:
            for name in ("Read", "ListDir", "Glob", "Grep"):
                if name not in enabled_tools:
                    enabled_tools.append(name)
            if self.file_allow_write:
                for name in ("Write", "Edit"):
                    if name not in enabled_tools:
                        enabled_tools.append(name)
            else:
                enabled_tools = [name for name in enabled_tools if name not in {"Write", "Edit"}]
        else:
            enabled_tools = [
                name
                for name in enabled_tools
                if name not in {"Read", "Write", "Edit", "ListDir", "Glob", "Grep"}
            ]
        if depth >= self.max_depth and "Agent" in enabled_tools:
            enabled_tools = [name for name in enabled_tools if name != "Agent"]
        if enabled_tools != list(config.tools):
            config = GeneralAgentConfig(
                name=config.name,
                system_prompt=config.system_prompt,
                tools=tuple(enabled_tools),
                tool_parameters=config.tool_parameters,
                max_turns=config.max_turns,
                role=config.role,
                skills=config.skills,
                when_to_use=config.when_to_use,
                system_prompt_template=config.system_prompt_template,
                system_prompt_args=config.system_prompt_args,
                metadata=config.metadata,
            )
        subagent_sink = self.log_session.create_subagent_sink(agent_type) if self.log_session is not None else None
        try:
            agent = GeneralPurposeAgent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=subagent_sink,
                stop_event=self.stop_event,
            )
            result = agent.run(prompt, description=description)
        finally:
            if subagent_sink is not None:
                subagent_sink.close()
            if self.execution_gateway is not None:
                self.execution_gateway.close_session(session_id)
        return session_id, result

    def _build_subagent_registry(self, *, depth: int, session_id: str, config: GeneralAgentConfig) -> ToolRegistry:
        """子 agent 的工具集：第二层基础工具 + workflow 专属 RunPython/RunWolfram。

        当 ``file_access_factory`` 提供时直接走第二层 ``build_default_tool_registry``；
        否则用空 registry（基础文件工具由 ``enabled_tools`` 过滤逻辑剔除）。差异化
        措辞统一由 agent YAML 的 ``tool_descriptions`` 表达，本方法不再重复注册基础工具。
        """
        if self.file_access_factory is not None:
            access = self.file_access_factory()
            registry = build_default_tool_registry(access)
        else:
            registry = ToolRegistry()
        python_env: dict[str, Any] = {}
        wolfram_session = {"session": None}

        registry.register(
            name="RunPython",
            description="Executes Python code in a persistent in-memory environment without filesystem access.\n\nUsage:\n- Run Python/SymPy/NumPy/SciPy code for symbolic/numeric computation.\n- The Python environment persists across calls within the same session.\n- No filesystem access is permitted; use file tools separately if needed.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "The Python code to execute."},
                },
                "required": ["code"],
            },
            handler=lambda args: _python_tool(
                args,
                python_env,
                execution_gateway=self.execution_gateway,
                session_id=session_id,
            ),
        )
        registry.register(
            name="RunWolfram",
            description="Execute Wolfram Language code in a short-lived Wolfram session when Wolfram is available.",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "The Wolfram Language code to execute."},
                },
                "required": ["code"],
            },
            handler=lambda args: _wolfram_tool(
                args,
                wolfram_session,
                execution_gateway=self.execution_gateway,
                session_id=session_id,
            ),
        )
        if depth < self.max_depth:
            register_agent_tool(
                registry,
                agent_config=config,
                dispatcher=self,
                depth=depth + 1,
            )
        return registry

    def _make_session_id(self, *, agent_type: str, depth: int) -> str:
        prefix = self.session_prefix.strip("/") or "subagent"
        return f"{prefix}/{agent_type}/depth-{depth}/{uuid.uuid4().hex}"


def _python_tool(
    args: dict[str, Any],
    env: dict[str, Any],
    *,
    execution_gateway: "ExecutionGateway | None",
    session_id: str,
) -> ToolResult:
    code = str(args.get("code") or "")
    if execution_gateway is not None:
        result = execution_gateway.run_python(session_id=session_id, code=code, allow_filesystem=False)
        return ToolResult(result.tool_content, is_error="[error]" in result.tool_content)
    stdout, error = run_python(code, env=env, allow_filesystem=False)
    payload = {}
    if stdout:
        payload["stdout"] = stdout
    if error:
        payload["error"] = error
    return ToolResult(json.dumps(payload, ensure_ascii=False), is_error=bool(error))


def _wolfram_tool(
    args: dict[str, Any],
    session_ref: dict[str, Any],
    *,
    execution_gateway: "ExecutionGateway | None",
    session_id: str,
) -> ToolResult:
    code = str(args.get("code") or "")
    if execution_gateway is not None:
        result = execution_gateway.run_wolfram(session_id=session_id, code=code)
        return ToolResult(result.tool_content, is_error="[error]" in result.tool_content)
    try:
        if session_ref.get("session") is None:
            from wolframclient.evaluation import WolframLanguageSession

            session_ref["session"] = WolframLanguageSession()
        output, error = run_wolfram(code, session=session_ref["session"])
    except Exception as exc:
        return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
    payload = {}
    if output:
        payload["output"] = output
    if error:
        payload["error"] = error
    return ToolResult(json.dumps(payload, ensure_ascii=False), is_error=bool(error))
