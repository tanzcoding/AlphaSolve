"""第三层 agent 工具装配的权威入口。

第二层只提供通用 coding agent runtime 和 ToolRegistry 注册接口；本模块负责把
AlphaSolve 研究工作区、执行网关、编排等第三层工具注册到同一个 registry。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from alphasolve.agent import AgentConfig, WorkspaceLike
from alphasolve.agent.tools import ToolRegistry, ToolResult, build_default_tool_registry, register_agent_tool
from alphasolve.agent.workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES
from .difficulty_dag import DifficultyDagStore, register_curated_difficulty_dag_tool
from .difficulty_declaration import register_difficulty_declaration_tool
from .research_markdown import (
    _markdown_index_progress_audit_hint,
    _markdown_knowledge_index_hint,
    _markdown_read_review_hint,
    _markdown_statement_tail_preview,
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
            "- In AlphaSolve research workspaces, this is an exact file-inspection tool, not the primary way to survey progress across many propositions or notes.\n"
            "- For broad progress review over `problem.md`, `verified_propositions/`, and `knowledge/`, run ResearchProgressReview first; then use Read on the cited files that need line-level precision.\n"
            "- A `<system>` tag will be given before the read file content.\n"
            "- The system will notify you when there is anything wrong when reading the file.\n"
            "- This tool is worth using in parallel only after a progress map or explicit citation has narrowed the files to inspect.\n"
            "- For Markdown proposition files with Statement and Proof sections, the default call with only `path` returns the Statement section plus the file tail; set `read_all=true` or pass `line_offset`/`n_lines` for exact proof inspection.\n"
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
            "- Also use it to distinguish local cleanups from the current global blocker: theorem-level assembly gaps, unresolved restrictions, missing constant checks, and already-verified infrastructure that should not be reproved.\n"
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
            "- Prefer inspecting cited blocker, obstruction, index, and synthesis files before choosing a local lemma as the next global step.\n"
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
    registry.register(
        name="SplitReference",
        description=(
            "Split a large user-provided Markdown reference into smaller Markdown files without rewriting its text.\n\n"
            "Usage:\n"
            "- Use this only for files under `knowledge/references/`.\n"
            "- Each part copies an exact inclusive line range from the source file into a new Markdown file.\n"
            "- SplitReference never edits the source file and never rewrites, summarizes, or paraphrases reference text.\n"
            "- Create destination folders first with MakeDir when needed.\n"
            "- Use this when a long reference file is hard for LLM agents to read as one piece."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Existing Markdown reference file to split.",
                },
                "parts": {
                    "type": "array",
                    "description": (
                        "Split parts. Each object must have path, start_line, and end_line. "
                        "Line ranges are 1-based and inclusive."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "New Markdown file path for this part."},
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                        },
                        "required": ["path", "start_line", "end_line"],
                    },
                },
            },
            "required": ["source_path", "parts"],
        },
        handler=lambda args: ToolResult(
            json.dumps(workspace.split_reference_file(args["source_path"], args["parts"]), ensure_ascii=False)
        ),
    )


def _run_research_read(workspace: WorkspaceLike, args: dict[str, Any]) -> ToolResult:
    explicit_range = (
        int(args.get("line_offset", 1)) != 1
        or int(args.get("n_lines", READ_PAGE_DEFAULT_LINES)) != READ_PAGE_DEFAULT_LINES
        or bool(args.get("read_all", False))
    )
    use_statement_tail_preview = not explicit_range and str(args["path"]).lower().endswith((".md", ".markdown"))
    read_args = dict(args)
    if use_statement_tail_preview:
        read_args["line_offset"] = 1
        read_args["read_all"] = True
    try:
        result = workspace.read_text_page(
            read_args["path"],
            line_offset=int(read_args.get("line_offset", 1)),
            n_lines=int(read_args.get("n_lines", READ_PAGE_DEFAULT_LINES)),
            read_all=bool(read_args.get("read_all", False)),
        )
    except Exception as exc:
        return ToolResult(f"<system>ERROR reading {args['path']}: {exc}</system>", is_error=True)
    system = f"path: {args['path']}\n{result.message}"
    if not result.output:
        return ToolResult(f"<system>{system}</system>")
    parts = [f"<system>{system}</system>"]
    hint = _markdown_read_review_hint(str(args["path"]), result.output, result.message)
    index_audit_hint = _markdown_index_progress_audit_hint(workspace, str(args["path"]))
    knowledge_index_hint = _markdown_knowledge_index_hint(workspace, str(args["path"]))
    if index_audit_hint:
        parts.append(index_audit_hint)
    if knowledge_index_hint:
        parts.append(knowledge_index_hint)
    if hint:
        parts.append(hint)
    preview = _markdown_statement_tail_preview(result.output) if use_statement_tail_preview else ""
    if preview:
        parts.append(
            "<system>Default research Read preview: this Markdown file has Statement and Proof sections, "
            "so only the Statement section and file tail are shown. Use read_all=true or pass line_offset/n_lines "
            "to inspect the full proof.</system>"
        )
        parts.append(preview)
    else:
        parts.append(result.output)
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
            "- No filesystem access is permitted; use file tools separately if needed.\n"
            '- For regexes or strings containing backslashes, use raw strings (for example `r"\\{"`) '
            'or double escaping (for example `"\\\\{"`); never write `"\\{"`.'
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
                "difficulty_id": {
                    "type": "string",
                    "description": (
                        "Optional provenance label for a known difficulty. It is not a dispatch prerequisite and does not "
                        "create, edit, or validate canonical DAG structure."
                    ),
                },
                "followup_handoff_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional worker-local handoff IDs from TaskOutput.local_difficulties that justify this bounded follow-up. "
                        "Each cited handoff may be followed up once per orchestrator run."
                    ),
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional workspace-relative evidence paths that the worker should inspect, such as a handoff, proposition, "
                        "review, theorem check, or process audit. These are evidence references, not mathematical facts by themselves."
                    ),
                },
                "method_id": {
                    "type": "string",
                    "enum": ["direct_proof", "contradiction", "construction", "computation", "falsification", "consolidation"],
                    "description": "Optional proof/search method for the selected difficulty leaf.",
                },
                "frontier_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional. A curated list of verified-proposition references (paths relative to "
                        "`verified_propositions` without the `.md` extension, subdirectories with backslashes, "
                        "e.g. `structural\\anchor-graph\\anchor-graph-for-rectangle-partition`) that define the "
                        "research frontier this worker should build on. The runtime injects each referenced "
                        "proposition's Statement into the worker's task so the worker can focus on its assigned "
                        "mathematical obligation instead of broad workspace exploration. Differentiate this per direction: give "
                        "an exploit spawn its mainline frontier, give a new/orthogonal direction a small orthogonal "
                        "set, and leave it empty for a free-exploration direction that should start from scratch."
                    ),
                },
                "frontier_note": {
                    "type": "string",
                    "description": (
                        "Optional. One short note telling the worker what the curated frontier means for this hint "
                        "or what to avoid (e.g. 'avoid the anchor-graph mainline; try construction/number-theoretic "
                        "structure such as 45^2'). Shown alongside frontier_refs."
                    ),
                },
                "rubric": {
                    "type": "string",
                    "description": (
                        "Optional. A concise acceptance checklist (3-6 bullet points, each starting with '- ') "
                        "specifying what the proven Statement must satisfy to count as progress on the assigned difficulty. "
                        "The rubric is not shown to the worker; it is retained in the immutable worker outcome ledger "
                        "for later curator review. "
                        "Example: '- Explicit construction of a permutation and tiling\n"
                        "- Rectangle count k <= 2111\n"
                        "- All non-hole cells covered exactly once'. "
                        "Write criteria that are checkable against the proven Statement alone."
                    ),
                },
                "consolidation": {
                    "type": "boolean",
                    "description": (
                        "Reserved for global_attack=true. Local leaves, local assembly, and parent-direct attempts "
                        "must leave this false so they can record a strict child, missing bridge, method block, or "
                        "refutation. The runtime rejects consolidation=true without global_attack=true."
                    ),
                },
                "pinned_target": {
                    "type": "string",
                    "description": (
                        "Optional target context. global_attack uses it as the fixed original-problem target; local "
                        "assembly or parent-direct work may retain it as the current focus but may still weaken into "
                        "a strict child difficulty."
                    ),
                },
                "global_attack": {
                    "type": "boolean",
                    "description": (
                        "Optional (default false). Set true to launch a GLOBAL consolidation that attacks "
                        "`problem.md` directly — not any canonical difficulty leaf. This implies "
                        "consolidation=true (no weakening allowed). The runtime permits the first attack and each later "
                        "attack only after the configured number "
                        "of verified propositions has accumulated; a completed attack resets that count. Every completed global "
                        "attack is reported to the curator for knowledge and later DAG curation. If pinned_target is omitted, the runtime "
                        "reads problem.md into pinned_target automatically. Do NOT pass difficulty_id with global_attack."
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
            "Return content is JSON. It always includes completed, active_count, active_worker_ids, active_workers, max_workers, and available_worker_slots. "
            "Completed workers may include a worker-local difficulty_handoff plus direct proposition, review, and verification artifacts. "
            "When available, local_difficulties lists compact worker and reasoning-subagent obstacle handoffs. For one concrete "
            "follow-up, use SpawnWorker with followup_handoff_ids and evidence_refs; each handoff may be cited once per run. "
            "Use RequestResearchPlan when local evidence does not justify a bounded continuation or the strategic direction is unclear. "
            "Canonical parent/child structure remains curator-owned. "
            "It may include timed_out when no worker finishes before the timeout; solved and solution_path when the original problem is solved; "
            "human_expert_updates when hint.md or knowledge/references changed during the run; and progress_audit with the independent process auditor's latest verdict and persisted checkpoint files. "
            "When this collection triggers a checkpoint, it waits for that process-audit decision and returns a bounded process_audit_decisions summary; read its audit_path or evidence_path only when exact evidence is needed."
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
    read_state_resolver: Callable[[str, Any], bool | None] | None = None,
    difficulty_record_context: dict[str, Any] | None = None,
) -> ToolRegistry:
    """构造第三层最终 ToolRegistry。"""
    registry = build_default_tool_registry(workspace)
    register_research_markdown_tools(registry, workspace)
    if agent_config is not None and agent_config.name == "curator":
        workspace_root = getattr(getattr(workspace, "workspace", None), "root", None)
        if workspace_root:
            difficulty_dag = DifficultyDagStore(workspace_root)
            register_curated_difficulty_dag_tool(
                registry,
                difficulty_dag=difficulty_dag,
                checkpoint_id=None,
            )
    for registrar in extra_registrars:
        registrar(registry)
    if agent_config is not None and agent_config.name in {"generator", "reviser", "reasoning_subagent"}:
        worker_rel = getattr(workspace, "worker_rel", None)
        workspace_root = getattr(getattr(workspace, "workspace", None), "root", None)
        if worker_rel and workspace_root:
            register_difficulty_declaration_tool(
                registry,
                declaration_path=workspace_root / worker_rel / "difficulty_declaration.md",
                role=agent_config.name,
                record_context=difficulty_record_context,
            )
    if agent_config is not None and dispatcher is not None:
        register_agent_tool(
            registry,
            agent_config=agent_config,
            dispatcher=dispatcher,
            read_state_resolver=read_state_resolver,
        )
    return registry
