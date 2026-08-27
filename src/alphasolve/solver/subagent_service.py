"""第三层子 agent 调度：递归创建子 agent、注入 RunPython/RunWolfram 等执行工具。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agent import (
    AgentRunResult,
    AgentConfig,
    Agent,
    AgentContextPolicy,
    ToolRegistry,
    ToolResult,
)
from alphasolve.agent.tools import register_agent_tool
from alphasolve.solver.execution.runners import run_python, run_wolfram

from .client_factory import ClientFactory
from .context_policies import context_policy_for_subagent
from .logging.event_log import compose_event_sinks
from .tool_runtime import build_solver_tool_registry, clone_agent_config_with_tools, register_execution_tools
from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from alphasolve.solver.execution import ExecutionGateway


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
    # The reviewer may consult symbolic/numerical computation and adversarial reasoning
    # repeatedly during one strategic review.  The ordinary recursive-depth guard still
    # prevents those delegates from expanding into an unbounded agent tree.
    REVIEWER_UNLIMITED_DELEGATE_TYPES = {
        "compute_subagent",
        "reasoning_subagent",
    }
    REVIEWER_LIMITED_DELEGATE_LIMITS = {
        "numerical_experiment_subagent": 1,
    }
    # A reviewer may delegate mathematical checking, but delegates never become a
    # second strategy owner.  The reasoning delegate receives this policy contract so
    # it can adversarially test all five search levels and the proposed tabu boundary.
    _REVIEWER_POLICY_DELEGATE_TYPES = {"reasoning_subagent"}

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
        context_policy_factory: Callable[[str], AgentContextPolicy | None] | None = None,
        reviewer_history_path: "Path | None" = None,
        reviewer_state_provider: Callable[[], dict[str, Any] | None] | None = None,
        call_guard: Callable[[str, int], None] | None = None,
        allow_research_reviewer: bool = False,
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
        # 按 agent_type 返回 context_policy；默认用 context_policy_for_subagent。
        # 调用方可传 None 完全禁用压缩，或传自定义 factory 覆盖默认行为。
        self.context_policy_factory = (
            context_policy_factory if context_policy_factory is not None
            else context_policy_for_subagent
        )
        # research_reviewer 跨调用记忆：每次 reviewer 返回后，把 final_answer 追加到此文件。
        # 下次 reviewer 启动时读这个文件，知道前几次 reviewer 推荐了什么、发现了什么。
        self.reviewer_history_path = reviewer_history_path
        # dispatch frontier 由 orchestrator 显式注入 reviewer；它是受限投影，不依赖 reviewer 扫描原始 DAG。
        self.reviewer_state_provider = reviewer_state_provider
        # 调用方可用此门禁限制某个 subagent 的可调用阶段；门禁抛出的异常会作为工具错误返回。
        self.call_guard = call_guard
        # Reviewer 是 orchestrator 专属的全局战略角色。默认拒绝，以防某个角色配置
        # 遗漏 Agent.type.enum 时通过工具默认值意外暴露它。
        self.allow_research_reviewer = bool(allow_research_reviewer)
        self._reviewer_delegate_budget = threading.local()

    def available_types(self) -> list[str]:
        types = sorted(self.suite.subagents)
        if not self.allow_research_reviewer:
            types = [agent_type for agent_type in types if agent_type != "research_reviewer"]
        return types

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

    def _reserve_reviewer_delegate(self, agent_type: str) -> None:
        budget = getattr(self._reviewer_delegate_budget, "value", None)
        if budget is None:
            return
        if agent_type in self.REVIEWER_UNLIMITED_DELEGATE_TYPES:
            return
        if agent_type not in self.REVIEWER_LIMITED_DELEGATE_LIMITS:
            raise PermissionError(
                "research_reviewer may delegate only to compute_subagent, reasoning_subagent, or numerical_experiment_subagent"
            )
        remaining = budget.get(agent_type, self.REVIEWER_LIMITED_DELEGATE_LIMITS[agent_type])
        if remaining <= 0:
            raise RuntimeError(f"research_reviewer delegation budget exhausted for {agent_type}")
        budget[agent_type] = remaining - 1

    def call(self, agent_type: str, description: str, prompt: str, *, depth: int = 0) -> str:
        if agent_type == "research_reviewer" and not self.allow_research_reviewer:
            raise PermissionError("research_reviewer is reserved for orchestrator global strategic review")
        self._reserve_reviewer_delegate(agent_type)
        if self.call_guard is not None:
            self.call_guard(agent_type, depth)
        if self._is_reviewer_policy_delegate(agent_type):
            prompt = self._reviewer_policy_delegate_prompt(prompt)
        if (
            agent_type == "research_reviewer"
            and self.reviewer_history_path is not None
            and "## Prior Reviewer Decisions" not in prompt
        ):
            history = self._recent_reviewer_history()
            if history:
                prompt = (
                    "## Prior Reviewer Decisions\n\n"
                    "These are your own recent research plans and tracks in this project. They are fallible and may be "
                    "outdated; use them to avoid re-recommending a route you already rejected, and to state explicitly "
                    "what new evidence justifies reversing one.\n\n"
                    f"{history}\n\n---\n\n"
                    + prompt
                )
        if (
            agent_type == "research_reviewer"
            and self.reviewer_state_provider is not None
            and "## Planning Input" not in prompt
        ):
            try:
                snapshot = self.reviewer_state_provider()
            except Exception as exc:
                snapshot = {"state_snapshot_error": f"{type(exc).__name__}: {exc}"}
            if snapshot:
                prompt = (
                    "# Canonical Research Graph Snapshot\n\n"
                    "This is a read-only canonical graph projection. Use it with verified propositions and knowledge; "
                    "historical prose remains fallible.\n\n"
                    f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n```\n\n"
                    + prompt
                )
        session_id, result = self._run(agent_type, description, prompt, depth=depth)
        self._submit_curator_trace(
            agent_type=agent_type,
            session_id=session_id,
            description=description,
            prompt=prompt,
            result=result,
        )
        # research_reviewer 跨调用记忆：把最终报告追加到 history 文件。它可以反复咨询
        # reasoning/compute 子代理，并对数值实验保持独立预算；递归深度仍由运行时限制。
        if agent_type == "research_reviewer" and self.reviewer_history_path is not None:
            self._append_reviewer_history(
                session_id=session_id,
                description=description,
                result=result,
            )
        return _format_subagent_result(
            agent_type=agent_type,
            session_id=session_id,
            text=_last_plain_assistant_content(result),
        )

    def _is_reviewer_policy_delegate(self, agent_type: str) -> bool:
        return (
            agent_type in self._REVIEWER_POLICY_DELEGATE_TYPES
            and getattr(self._reviewer_delegate_budget, "value", None) is not None
        )

    @staticmethod
    def _reviewer_policy_delegate_prompt(prompt: str) -> str:
        """Scope a reasoning delegate as an adversarial reviewer aide, not a strategist.

        The reviewer must include the concrete candidate track or strategy in its
        delegated prompt.  This wrapper makes the five-level protocol and the taboo
        semantics explicit without manufacturing a plan from incomplete context.
        """
        return (
            "# Reviewer Delegation Contract\n\n"
            "You are an adversarial reasoning delegate of the research reviewer. The reviewer, not you, owns final "
            "route selection, tabu, parking, graph departure, and synthesis decisions. Audit only the candidate strategy, "
            "track, or premise supplied below; do not invent a replacement plan when that material is absent.\n\n"
            "Check whether the supplied classification is justified: LOCAL_REPAIR requires a named local bridge; NODE_ROUTE "
            "must change mechanism while retaining one obligation; GRAPH_PORTFOLIO must genuinely compare/attack the named "
            "nodes or ancestor; TECHNIQUE_EXPLORATION must change framework and state the terminal obligation; GLOBAL_SYNTHESIS "
            "must combine evidence against the original problem rather than rename a local route.\n\n"
            "Check tabu claims separately: hard tabu needs verified evidence that refutes its premise and a reopen condition; "
            "soft tabu must not be mistaken for refutation; hint is reusable context only. Flag a proposed route that merely "
            "renames a tabu mechanism, omits a known counterexample scope, lacks a discriminating first artifact, or treats "
            "unchanged attempts as proof that a statement is false. Report evidence for or against the candidate, uncovered "
            "assumptions, counterexamples to seek, and any missing reopen condition. End with VERDICT: CLEAR or VERDICT: "
            "BLOCKING_FOUND.\n\n"
            "# Delegated Reviewer Check\n\n"
            + prompt
        )

    REVIEWER_HISTORY_ENTRIES = 3
    REVIEWER_HISTORY_MAX_CHARS = 6000

    def _recent_reviewer_history(self) -> str:
        """Return the most recent reviewer reports so strategy does not oscillate.

        Only the trailing entries are injected, and only their `Research Strategy JSON`
        section when present, so the reviewer sees what it previously decided without
        re-reading full adversarial-review prose.
        """
        path = self.reviewer_history_path
        if path is None or not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        entries = [item.strip() for item in text.split("\n---\n\n") if item.strip()]
        if not entries:
            return ""
        selected = entries[-self.REVIEWER_HISTORY_ENTRIES:]
        rendered: list[str] = []
        for entry in selected:
            marker = "### Research Strategy JSON"
            index = entry.find(marker)
            if index != -1:
                header = entry[: entry.find("\n\n")] if "\n\n" in entry else ""
                rendered.append((header + "\n\n" + entry[index:]).strip())
            else:
                rendered.append(entry)
        joined = "\n\n---\n\n".join(rendered)
        if len(joined) > self.REVIEWER_HISTORY_MAX_CHARS:
            joined = joined[-self.REVIEWER_HISTORY_MAX_CHARS:]
        return joined

    def _append_reviewer_history(
        self,
        *,
        session_id: str,
        description: str,
        result: AgentRunResult,
    ) -> None:
        """把 reviewer 的最终报告追加到 history 文件，供下次 reviewer 读取。"""
        try:
            text = _last_plain_assistant_content(result)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            entry = (
                f"\n---\n\n## Reviewer call: {timestamp}\n"
                f"session: {session_id}\n"
                f"description: {description}\n\n"
                f"{text}\n"
            )
            self.reviewer_history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.reviewer_history_path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
        except Exception:
            # 历史记录是 best-effort，不能因为写文件失败影响 reviewer 主流程
            pass

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

    def _effective_max_depth(self, agent_type: str, depth: int) -> int:
        """一般由 ``self.max_depth`` 决定这次调用还能否再用 `Agent` 工具往下委派。

        ``research_reviewer`` 是唯一例外：不管 ``self.max_depth``（orchestrator 直调
        其它 subagent 时通常是 0，禁止再委派）取值如何，reviewer 自己总能拿到一层
        委派能力——可按需调用 `Agent(type="reasoning_subagent")`、
        `Agent(type="compute_subagent")` 做核验，并调用 `Agent(type="numerical_experiment_subagent")`
        进行受限数值检查。
        这一层不会继续放宽：reviewer 委派出去的下一层调用（agent_type 不是
        research_reviewer）仍然只服从 ``self.max_depth``，因此不可能出现委派预算
        逐层膨胀的无限递归。且 research_reviewer 自身不在它能调用的类型枚举里
        （见 ``research_reviewer.yaml`` 的 `tool_parameters.Agent.type.enum`），
        所以它也不能靠反复自调把这层预算再叠加一次。
        """
        if agent_type == "research_reviewer":
            return max(self.max_depth, depth + 1)
        return self.max_depth

    def _run(self, agent_type: str, description: str, prompt: str, *, depth: int = 0) -> tuple[str, AgentRunResult]:
        config = self.suite.subagents.get(agent_type)
        if config is None:
            allowed = ", ".join(self.available_types())
            raise ValueError(f"unknown subagent type: {agent_type}. Allowed types: {allowed}")
        session_id = self._make_session_id(agent_type=agent_type, depth=depth)
        effective_max_depth = self._effective_max_depth(agent_type, depth)
        registry = self._build_subagent_registry(
            depth=depth,
            session_id=session_id,
            config=config,
            max_depth=effective_max_depth,
            delegated_description=description,
            delegated_task=prompt,
        )
        enabled_tools = list(config.tools)
        # TODO(B-phase): 这段在 Python 里硬过滤 subagent 能用的文件/Agent 工具，
        # 是 A 阶段 Task 8 之后第三层仅剩的运行时工具白名单逻辑。B 阶段会让
        # extension API 用更通用的方式表达"按 file_access_factory / 递归 depth
        # 决定的运行时工具开关"。参见 plan §8。
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
        if depth >= effective_max_depth and "Agent" in enabled_tools:
            enabled_tools = [name for name in enabled_tools if name != "Agent"]
        if enabled_tools != list(config.tools):
            config = clone_agent_config_with_tools(config, enabled_tools)
        subagent_sink = self.log_session.create_subagent_sink(agent_type) if self.log_session is not None else None
        # R2：把该 subagent 的 token 用量计入共享聚合器，按“父角色-subagent/类型”归组。
        event_sink = subagent_sink
        if self.log_session is not None:
            parent = "orchestrator" if self.session_prefix.startswith("orchestrator") else "worker"
            token_sink = self.log_session.token_usage_sink(f"{parent}-subagent/{agent_type}")
            # 统一运行日志：逐次调用明细（token + CoT + 输出），同样按父角色-subagent/类型归组。
            run_sink = self.log_session.run_log_sink(f"{parent}-subagent/{agent_type}")
            event_sink = compose_event_sinks(subagent_sink, token_sink, run_sink)
        previous_reviewer_budget = getattr(self._reviewer_delegate_budget, "value", None)
        if agent_type == "research_reviewer":
            self._reviewer_delegate_budget.value = dict(self.REVIEWER_LIMITED_DELEGATE_LIMITS)
        try:
            # 按 agent_type 决定是否注入上下文压缩策略
            context_policy = None
            if self.context_policy_factory is not None:
                try:
                    context_policy = self.context_policy_factory(agent_type)
                except Exception:
                    context_policy = None
            agent = Agent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=event_sink,
                stop_event=self.stop_event,
                context_policy=context_policy,
            )
            result = agent.run(prompt, description=description)
        finally:
            if agent_type == "research_reviewer":
                if previous_reviewer_budget is None:
                    delattr(self._reviewer_delegate_budget, "value")
                else:
                    self._reviewer_delegate_budget.value = previous_reviewer_budget
            if subagent_sink is not None:
                subagent_sink.close()
            if self.execution_gateway is not None:
                self.execution_gateway.close_session(session_id)
        return session_id, result

    def _build_subagent_registry(
        self,
        *,
        depth: int,
        session_id: str,
        config: AgentConfig,
        max_depth: int,
        delegated_description: str,
        delegated_task: str,
    ) -> ToolRegistry:
        """子 agent 的工具集：第三层基础工具 + RunPython/RunWolfram。

        当 ``file_access_factory`` 提供时直接走 ``build_solver_tool_registry``；
        否则用空 registry（基础文件工具由 ``enabled_tools`` 过滤逻辑剔除）。差异化
        措辞统一由 agent YAML 的 ``tool_descriptions`` 表达，本方法不再重复注册基础工具。

        ``max_depth`` 由调用方（``_run``）用 ``_effective_max_depth`` 算好传入，一般等于
        ``self.max_depth``，但对 research_reviewer 会额外放宽一层，见该方法的说明。
        """
        if self.file_access_factory is not None:
            access = self.file_access_factory()
            registry = build_solver_tool_registry(
                access,
                agent_config=config,
                difficulty_record_context={
                    "delegated_description": delegated_description,
                    "delegated_task": delegated_task,
                    "subagent_session_id": session_id,
                },
            )
        else:
            registry = ToolRegistry()
        python_env: dict[str, Any] = {}
        wolfram_session = {"session": None}

        register_execution_tools(
            registry,
            run_python_handler=lambda args: _python_tool(
                args,
                python_env,
                execution_gateway=self.execution_gateway,
                session_id=session_id,
            ),
            run_wolfram_handler=lambda args: _wolfram_tool(
                args,
                wolfram_session,
                execution_gateway=self.execution_gateway,
                session_id=session_id,
            ),
        )
        if depth < max_depth:
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
