"""第三层 agent 工具装配的权威入口。

第二层只提供通用 coding agent runtime 和 ToolRegistry 注册接口；本模块负责把
AlphaSolve 研究工作区、执行网关、编排等第三层工具注册到同一个 registry。
"""
from __future__ import annotations

from typing import Any, Callable

from alphasolve.agent import AgentConfig, WorkspaceLike
from alphasolve.agent.tools import ToolRegistry, ToolResult, build_default_tool_registry, register_agent_tool
from alphasolve.agent.workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES
from .research_markdown import (
    _markdown_index_progress_audit_hint,
    _markdown_read_review_hint,
    _run_inspect_markdown,
    _run_research_progress_review,
)


ToolRegistrar = Callable[[ToolRegistry], None]


def clone_agent_config_with_tools(config: AgentConfig, tools: list[str] | tuple[str, ...]) -> AgentConfig:
    """复制 AgentConfig，只替换运行期可用工具列表。"""
    return AgentConfig(
        name=config.name,
        system_prompt=config.system_prompt,
        tier=config.tier,
        tools=tuple(tools),
        tool_parameters=config.tool_parameters,
        tool_descriptions=config.tool_descriptions,
        max_turns=config.max_turns,
        skills=config.skills,
        when_to_use=config.when_to_use,
        system_prompt_template=config.system_prompt_template,
        system_prompt_args=config.system_prompt_args,
        metadata=config.metadata,
    )


def register_research_markdown_tools(registry: ToolRegistry, workspace: WorkspaceLike) -> None:
    """注册 AlphaSolve 研究/证明工作区导航工具。"""
    registry.register(
        name="Read",
        description=(
            "Read text content from a file.\n\n"
            "Tips:\n"
            "- A `<system>` tag will be given before the read file content.\n"
            "- The system will notify you when there is anything wrong when reading the file.\n"
            "- This tool is typically worth using in parallel when you need to inspect multiple files.\n"
            "- If you want to search for a certain content or pattern, prefer Grep over Read.\n"
            "- Content will be returned with a line number before each line like `cat -n` format.\n"
            f"- By default, Read returns {READ_PAGE_DEFAULT_LINES} lines.\n"
            "- `line_offset` is the first line to return.\n"
            f"- `n_lines` is how many lines to return in this call; default is {READ_PAGE_DEFAULT_LINES}.\n"
            "- Set `read_all=true` to ignore `n_lines` and read from `line_offset` to the end of the file.\n"
            f"- Without `read_all`, the maximum `n_lines` value is {READ_PAGE_MAX_LINES}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to read."},
                "line_offset": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "description": "The line number to start reading from.",
                },
                "n_lines": {
                    "type": "integer",
                    "default": READ_PAGE_DEFAULT_LINES,
                    "minimum": 1,
                    "maximum": READ_PAGE_MAX_LINES,
                    "description": f"How many lines to return. Defaults to {READ_PAGE_DEFAULT_LINES}, max {READ_PAGE_MAX_LINES}.",
                },
                "read_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, ignore n_lines and read to end of file.",
                },
            },
            "required": ["path"],
        },
        handler=lambda args: _run_research_read(workspace, args),
        replace=True,
    )
    registry.register(
        name="ResearchProgressReview",
        description=(
            "Audit a Markdown research/proof workspace before deciding current progress or next propositions.\n\n"
            "Usage:\n"
            "- Use this early when a workspace has problem.md, knowledge notes, and verified propositions.\n"
            "- Prefer this over broad InspectMarkdown or many manual reads when starting from a workspace root.\n"
            "- If the result cites several major directories or competing evidence areas, use scoped Agent calls to inspect those areas separately, then compare the reports in the main agent.\n"
            "- It scans Markdown proof files for a general failure mode: the proof tail establishes a stronger or more actionable conclusion than the Statement section records.\n"
            "- If it reports an underclaimed proof that affects an answer, best-known objective value or estimate, stopping condition, or planning premise, the next proposition should normally make that stronger conclusion explicit as a Statement before pursuing harder work.\n"
            "- This is a progress-navigation tool, not a verifier; it does not prove new claims."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace root or directory to audit.", "default": "."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional specific files/directories to scan instead of auto-detecting research folders.",
                    "default": [],
                },
                "max_files": {
                    "type": "integer",
                    "description": "Maximum Markdown files to scan.",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": [],
        },
        handler=lambda args: _run_research_progress_review(workspace, args),
    )
    registry.register(
        name="InspectMarkdown",
        description=(
            "Survey Markdown files and surface the parts most likely to summarize research progress.\n\n"
            "Usage:\n"
            "- Use this when reviewing a notes/proofs workspace before deciding what is already known or what to do next.\n"
            "- It lists headings, extracts statement/progress sections, and always shows the file tail because conclusions are often buried near proof endings.\n"
            "- Its progress audit highlights underclaimed proof tails; if one affects an answer, best-known objective value or estimate, stopping condition, or planning premise, make that conclusion explicit as a proposition statement before harder new work.\n"
            "- This is a navigation and review aid, not a verifier; use Read on the cited file/lines before relying on a claim."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Markdown file or directory to inspect.", "default": "."},
                "max_files": {
                    "type": "integer",
                    "description": "Maximum Markdown files to inspect when path is a directory.",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 100,
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "How many ending lines to show for each file.",
                    "default": 40,
                    "minimum": 1,
                    "maximum": 200,
                },
                "statement_lines": {
                    "type": "integer",
                    "description": "Maximum lines to show from each selected statement/progress section.",
                    "default": 80,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": [],
        },
        handler=lambda args: _run_inspect_markdown(workspace, args),
    )


def _run_research_read(workspace: WorkspaceLike, args: dict[str, Any]) -> ToolResult:
    try:
        result = workspace.read_text_page(
            args["path"],
            line_offset=int(args.get("line_offset", 1)),
            n_lines=int(args.get("n_lines", READ_PAGE_DEFAULT_LINES)),
            read_all=bool(args.get("read_all", False)),
        )
    except Exception as exc:
        return ToolResult(f"<system>ERROR reading {args['path']}: {exc}</system>", is_error=True)
    system = f"path: {args['path']}\n{result.message}"
    if not result.output:
        return ToolResult(f"<system>{system}</system>")
    parts = [f"<system>{system}</system>", result.output]
    hint = _markdown_read_review_hint(str(args["path"]), result.output, result.message)
    index_audit_hint = _markdown_index_progress_audit_hint(workspace, str(args["path"]))
    if hint:
        parts.append(hint)
    if index_audit_hint:
        parts.append(index_audit_hint)
    return ToolResult("\n".join(parts))


def register_execution_tools(
    registry: ToolRegistry,
    *,
    run_python_handler: Callable[[dict[str, Any]], ToolResult],
    run_wolfram_handler: Callable[[dict[str, Any]], ToolResult],
) -> None:
    """注册第三层计算工具。"""
    registry.register(
        name="RunPython",
        description=(
            "Executes Python code in a persistent in-memory environment without filesystem access.\n\n"
            "Usage:\n"
            "- Run Python/SymPy/NumPy/SciPy code for symbolic/numeric computation.\n"
            "- The Python environment persists across calls within the same session.\n"
            "- No filesystem access is permitted; use file tools separately if needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python code to execute."},
            },
            "required": ["code"],
        },
        handler=run_python_handler,
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
        handler=run_wolfram_handler,
    )


def register_orchestrator_worker_tools(
    registry: ToolRegistry,
    *,
    spawn_handler: Callable[[dict[str, Any]], ToolResult],
    wait_handler: Callable[[dict[str, Any]], ToolResult],
    default_wait_timeout_seconds: float,
) -> None:
    """注册 orchestrator 专属 worker 编排工具。"""
    registry.register(
        name="SpawnWorker",
        description=(
            "Start one worker and return immediately; this tool does not wait for the worker to finish.\n\n"
            "Worker lifecycle:\n"
            "- The worker first runs generator to draft one candidate proposition.\n"
            "- It then runs verifier; if verification fails and rounds remain, it runs reviser and repeats verifier -> reviser.\n"
            "- After verification, theorem-checking decides whether the verified proposition resolves the original problem.\n\n"
            "Return content is JSON containing whether a worker was spawned, plus active_count, active_worker_ids, active_workers, max_workers, and available_worker_slots. "
            "If the parallelism limit has been reached, call TaskOutput before spawning more workers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hint": {
                    "type": "string",
                    "description": (
                        "Optional targeted hint for this worker only. Suggest a direction, method, "
                        "branch, local target, or auxiliary assumption. This is different from the user's hint.md."
                    ),
                },
            },
            "required": [],
        },
        handler=spawn_handler,
    )
    registry.register(
        name="TaskOutput",
        description=(
            "Wait until one active worker finishes, or until the timeout is reached.\n\n"
            "Use this tool to collect worker lifecycle results. If the maximum number of active workers has been reached, call TaskOutput before spawning more workers.\n\n"
            "When you use this tool, if there is still an available worker slot, the orchestrator will start one worker for free exploration.\n\n"
            "Return content is JSON. It always includes completed, active_count, active_worker_ids, active_workers, max_workers, available_worker_slots, and free_exploration_worker. "
            "It may include timed_out when no worker finishes before the timeout; solved and solution_path when the original problem is solved; "
            "human_expert_updates when hint.md or knowledge/references changed during the run; and verified_propositions_organization when verified proposition directories should be organized before more spawning."
        ),
        parameters={
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Maximum seconds to wait before returning active worker status.",
                    "default": default_wait_timeout_seconds,
                    "minimum": 1200,
                    "maximum": 7200,
                },
            },
            "required": [],
        },
        handler=wait_handler,
    )


def build_solver_tool_registry(
    workspace: WorkspaceLike,
    *,
    agent_config: AgentConfig | None = None,
    dispatcher: Any | None = None,
    extra_registrars: tuple[ToolRegistrar, ...] = (),
) -> ToolRegistry:
    """构造第三层最终 ToolRegistry。"""
    registry = build_default_tool_registry(workspace)
    register_research_markdown_tools(registry, workspace)
    for registrar in extra_registrars:
        registrar(registry)
    if agent_config is not None and dispatcher is not None:
        register_agent_tool(registry, agent_config=agent_config, dispatcher=dispatcher)
    return registry
