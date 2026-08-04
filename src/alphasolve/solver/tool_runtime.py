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
from .blocker_registry import PersistentBlockerRegistry, register_curated_blocker_registry_tool
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
                "direction_id": {
                    "type": "string",
                    "description": "Stable id of the research direction in state.md that owns this worker.",
                },
                "gap_id": {
                    "type": "string",
                    "description": "Stable id of the open gap inside that direction which this worker attacks.",
                },
                "method_id": {
                    "type": "string",
                    "enum": ["direct_proof", "contradiction", "construction", "computation", "falsification", "consolidation"],
                    "description": "Optional proof/search method arm. If omitted, the hierarchical scheduler recommendation is used.",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Backward-compatible single parent attempt state_id. Direction arm ids are not attempt ids.",
                },
                "parent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional parent attempt state_ids for the attempt DAG. Pass multiple ids for crossover. "
                        "These describe lineage only; direction_id independently identifies the research direction."
                    ),
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
                "blocker_override_reason": {
                    "type": "string",
                    "description": (
                        "Required only when this direction has an active process-audit blocker and this spawn targets "
                        "a different gap. State the concrete new evidence that justifies pivoting instead of directly "
                        "attacking that blocker."
                    ),
                },
                "rubric": {
                    "type": "string",
                    "description": (
                        "Optional. A concise acceptance checklist (3-6 bullet points, each starting with '- ') "
                        "specifying what the proven Statement must satisfy to count as progress on the assigned gap. "
                        "The rubric is NOT shown to the worker — it is stored and returned to you in TaskOutput "
                        "for your own structured self-assessment via RecordResearchImpact. "
                        "Example: '- Explicit construction of a permutation and tiling\n"
                        "- Rectangle count k <= 2111\n"
                        "- All non-hole cells covered exactly once'. "
                        "Write criteria that are checkable against the proven Statement alone."
                    ),
                },
                "consolidation": {
                    "type": "boolean",
                    "description": (
                        "Optional (default false). Set true to spawn a CONSOLIDATION / DUAL attempt on a FIXED "
                        "target: the worker may NOT weaken the statement, narrow it, add fresh hypotheses, or "
                        "isolate a smaller sub-claim to pass verification. Only two results count as success: "
                        "proving the pinned target exactly as stated, or refuting it with an explicit checkable "
                        "witness (construction/counterexample). Use this to (a) cash out a direction whose verified "
                        "props have accumulated, by attacking its terminal goal head-on, or (b) run the dual/"
                        "falsification of a persistently-failing target. Pass the target text via pinned_target and "
                        "give the direction's mainline props via frontier_refs."
                    ),
                },
                "pinned_target": {
                    "type": "string",
                    "description": (
                        "Optional. The exact target statement to attack when consolidation=true. If omitted, the "
                        "worker uses the goal expressed in `hint`. Ignored when consolidation is false."
                    ),
                },
                "global_attack": {
                    "type": "boolean",
                    "description": (
                        "Optional (default false). Set true to launch a GLOBAL consolidation that attacks "
                        "`problem.md` directly — not any specific direction's terminal goal. This implies "
                        "consolidation=true (no weakening allowed). If pinned_target is omitted, the runtime "
                        "reads problem.md into pinned_target automatically. Use this when global_consolidation_directive.ready "
                        "is true and you want to test whether accumulated verified propositions can already close "
                        "the original problem. Do NOT pass direction_id or gap_id with global_attack."
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
            "Verified but unsolved completions also appear in selection_advice.pending_research_impacts and must be assessed with RecordResearchImpact before more targeted spawning. "
            "Consolidation workers that failed (rejected, or verified but did not achieve their pinned target) are auto-assessed by the runtime with consolidation_outcome=wall and appear with auto_assessed=true; they do NOT require RecordResearchImpact. "
            "Consolidation workers that are verified but have target_achieved=null DO require your RecordResearchImpact assessment (you must judge whether the proven statement matches the pinned target). "
            "It may include timed_out when no worker finishes before the timeout; solved and solution_path when the original problem is solved; "
            "human_expert_updates when hint.md or knowledge/references changed during the run; verified_propositions_organization when verified proposition directories should be organized before more spawning; and progress_audit with the independent process auditor's latest verdict, terminal gap, and persisted audit files. "
            "Completed workers with an unresolved mathematical handoff appear in difficulty_portfolio. When at least two candidate handoffs are available, the runtime requests an independent research_reviewer comparison and returns its report; use it to decide whether to attack a shared obligation, keep routes distinct, or pivot. A handoff is not a canonical blocker until curator curation confirms it. "
            "When process_audit_context_reset is present, the runtime will clear this orchestrator conversation before the next model request, automatically run one fresh independent research_reviewer assessment, and inject its report into the new context. Read the cited audit and reviewer report, verify their evidence, and SyncResearchState before targeted spawning."
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


def register_orchestrator_research_tools(
    registry: ToolRegistry,
    *,
    record_impact_handler: Callable[[dict[str, Any]], ToolResult],
    record_dispatch_constraint_handler: Callable[[dict[str, Any]], ToolResult],
    sync_state_handler: Callable[[dict[str, Any]], ToolResult],
) -> None:
    """注册 direction-level ResearchImpact 与战略状态同步工具。"""
    registry.register(
        name="RecordResearchImpact",
        description=(
            "Assess one completed verified worker result against the direction-level gap it was assigned. "
            "Use this after TaskOutput for every worker listed in pending_research_impacts, before spawning more targeted work. "
            "Correctness is already decided by the verifier; classify whether the correct result solves, directly advances, "
            "only gives a necessary condition, weakens, duplicates, refutes, or misses the target. "
            "gap_effect='closed' is only accepted when relation_to_target is solves_target, sufficient_for_target, "
            "or direct_advance; any other relation reporting gap_effect='closed' is rejected."
        ),
        parameters={
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Completed worker id from TaskOutput."},
                "relation_to_target": {
                    "type": "string",
                    "enum": [
                        "solves_target", "sufficient_for_target", "direct_advance",
                        "necessary_condition_only", "weaker_than_target", "refutes_target",
                        "orthogonal", "duplicate", "unrelated", "unknown",
                    ],
                },
                "gap_effect": {
                    "type": "string",
                    "enum": ["closed", "advanced", "unchanged", "invalidated"],
                },
                "closed_gap_ids": {"type": "array", "items": {"type": "string"}},
                "remaining_gaps": {"type": "array", "items": {"type": "string"}},
                "new_gaps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "gap_id": {"type": "string"},
                            "statement": {"type": "string"},
                        },
                    },
                },
                "continuation_value": {
                    "type": "string",
                    "enum": ["high", "medium", "low", "none"],
                },
                "requires_direction_review": {"type": "boolean"},
                "consolidation_outcome": {
                    "type": "string",
                    "enum": ["solved", "blocked_gap", "wall"],
                    "description": (
                        "Optional. Set only when assessing a consolidation/dual attempt (a worker spawned with "
                        "consolidation=true). 'solved' = the pinned target was proved as stated; 'blocked_gap' = "
                        "not proved but a concrete attackable gap was localized (record it in new_gaps); 'wall' = "
                        "the attempt repeatedly hit the same insurmountable point / could not connect the props to "
                        "the target — this flags the direction for a mandatory dual/falsification probe and lowers "
                        "its budget."
                    ),
                },
                "discharges_standing_hypothesis": {
                    "type": "boolean",
                    "description": (
                        "Optional (default false). For an impossibility-kind direction, set true only if this result "
                        "unconditionally discharges the direction's standing_hypothesis (e.g. eliminates a case "
                        "without assuming k<=T) rather than deriving one more property under it. If false, a "
                        "'direct_advance'/'sufficient_for_target' claim is auto-downgraded to necessary_condition_only, "
                        "because deriving another conditional property is not progress toward the contradiction."
                    ),
                },
                "rubric_assessment": {
                    "type": "object",
                    "description": (
                        "Optional. Structured self-assessment against the rubric that was attached to this worker's "
                        "SpawnWorker call. For each criterion in the rubric, state whether the proven Statement passes "
                        "it and cite evidence. The runtime tracks consecutive zero-pass workers per direction: 2+ in a "
                        "row automatically flags the direction for re-review."
                    ),
                    "properties": {
                        "checks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "criterion": {"type": "string"},
                                    "passed": {"type": "boolean"},
                                    "evidence": {"type": "string"},
                                },
                            },
                        },
                        "passed_count": {"type": "integer"},
                        "total_checks": {"type": "integer"},
                    },
                },
                "summary": {"type": "string"},
                "method_failure_type": {
                    "type": "string",
                    "enum": ["method_blocked", "conclusion_refuted"],
                    "description": (
                        "Optional. Tabu search: classify a failure (when relation_to_target is "
                        "necessary_condition_only/weaker_than_target/orthogonal/duplicate/unrelated/unknown "
                        "and continuation_value is low/none). "
                        "'method_blocked' = the method was insufficient but the goal may still be correct → "
                        "the runtime records the method_family in the direction's failed_methods (tabu list) "
                        "so future spawns must use a different method. "
                        "'conclusion_refuted' = the goal itself may be false → also records to tabu list and "
                        "flags the direction for review. "
                        "Requires failed_method_family to be set."
                    ),
                },
                "failed_method_family": {
                    "type": "string",
                    "description": (
                        "Required when method_failure_type is set. The method family that failed "
                        "(e.g., 'anchor-graph', 'induction', 'rsk', 'topological', 'lp-dual', "
                        "'information-theoretic', 'algebraic'). Recorded in the direction's "
                        "failed_methods tabu list."
                    ),
                },
            },
            "required": [
                "worker_id", "relation_to_target", "gap_effect", "continuation_value", "summary",
            ],
        },
        handler=record_impact_handler,
    )
    registry.register(
        name="RecordDispatchConstraint",
        description=(
            "Persist a reviewer or numerical-probe finding that controls future targeted dispatch for one exact "
            "direction/gap. Use status='refuted' only with a verified proof, explicit witness, or exhaustive finite "
            "counterexample; it invalidates that exact gap and blocks repeat dispatch. Use "
            "status='needs_falsification' for sampled/incomplete numerical evidence; ordinary proof workers are then "
            "blocked until a method_id='falsification' worker checks the claim. This is not a mathematical proof tool: "
            "it records the evidence scope so the scheduler cannot mistake a sample for a universal theorem."
        ),
        parameters={
            "type": "object",
            "properties": {
                "direction_id": {"type": "string"},
                "gap_id": {"type": "string"},
                "claim": {
                    "type": "string",
                    "description": "The exact claim whose dispatch policy is being constrained.",
                },
                "status": {
                    "type": "string",
                    "enum": ["refuted", "needs_falsification"],
                },
                "evidence_level": {
                    "type": "string",
                    "enum": ["verified", "explicit_witness", "exhaustive_finite", "sampled"],
                    "description": (
                        "'refuted' requires verified, explicit_witness, or exhaustive_finite evidence; sampled "
                        "evidence may only require a falsification probe."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "Compact counterexample or experiment scope, coverage, and conclusion.",
                },
                "source_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Workspace-relative files/logs that contain the evidence.",
                },
            },
            "required": ["direction_id", "gap_id", "claim", "status", "evidence_level", "evidence"],
        },
        handler=record_dispatch_constraint_handler,
    )
    registry.register(
        name="SyncResearchState",
        description=(
            "Replace the qualitative research plan with a structured portfolio of multiple directions. "
            "Each direction owns its gaps, progress summary, evidence, next actions, review trigger, and stop condition. "
            "The runtime preserves measured impact counters and atomically regenerates verified_propositions/state.md. "
            "Use index.md separately for verified proposition facts; never put strategic plans in index.md."
        ),
        parameters={
            "type": "object",
            "properties": {
                "objective_summary": {"type": "string"},
                "objective_status": {"type": "string"},
                "directions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "direction_id": {"type": "string"},
                            "title": {"type": "string"},
                            "goal": {"type": "string"},
                            "status": {"type": "string"},
                            "health": {"type": "string"},
                            "direction_kind": {
                                "type": "string",
                                "enum": [
                                    "unknown", "impossibility", "lower_bound", "upper_bound",
                                    "construction", "existence", "classification",
                                ],
                                "description": (
                                    "Optional. Set 'impossibility' for a direction that tries to prove a value optimal "
                                    "or a configuration impossible (e.g. 'k<=T is impossible'); then also give "
                                    "standing_hypothesis and terminal_gap_id so advance is judged honestly and the "
                                    "consolidation/dual machinery can engage."
                                ),
                            },
                            "standing_hypothesis": {
                                "type": "string",
                                "description": (
                                    "Optional. The assumption every result in an impossibility direction carries "
                                    "(e.g. 'k <= 3035'). A result that only derives another property under it, without "
                                    "discharging it or closing the terminal gap, will not count as advance."
                                ),
                            },
                            "terminal_gap_id": {
                                "type": "string",
                                "description": (
                                    "Optional. The gap_id of the direction's terminal obligation (the contradiction / "
                                    "final goal). Progress on peripheral gaps while this stays open is not advance."
                                ),
                            },
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step_id": {"type": "string"},
                                        "statement": {"type": "string"},
                                        "status": {"type": "string", "enum": ["open", "advanced", "closed", "invalidated"]},
                                        "method": {"type": "string"},
                                    },
                                },
                                "description": "Ordered research steps. Each step is both a plan item and a gap to close. Refine via SyncResearchState as workers return results.",
                            },
                            "progress_summary": {"type": "string"},
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Verified proposition paths relevant to this direction (relative, no extension).",
                            },
                            "knowledge_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Knowledge note paths relevant to this direction (relative, no extension).",
                            },
                            "stop_condition": {"type": "string"},
                            "failed_methods": {
                                "type": "array",
                                "description": (
                                    "Tabu list: method families that have failed on this direction's goal. "
                                    "Each entry: {method_family, failure_type, summary, gap_id, recorded_at}. "
                                    "The runtime preserves this across SyncResearchState calls; you usually "
                                    "do NOT need to set it manually — use RecordResearchImpact with "
                                    "method_failure_type instead. Only set explicitly when creating a new "
                                    "direction that inherits the tabu list of a retired one."
                                ),
                                "items": {"type": "object"},
                            },
                        },
                    },
                },
                "dispatch_plan": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["objective_summary", "directions"],
        },
        handler=sync_state_handler,
    )


def build_solver_tool_registry(
    workspace: WorkspaceLike,
    *,
    agent_config: AgentConfig | None = None,
    dispatcher: Any | None = None,
    extra_registrars: tuple[ToolRegistrar, ...] = (),
    read_state_resolver: Callable[[str, Any], bool | None] | None = None,
) -> ToolRegistry:
    """构造第三层最终 ToolRegistry。"""
    registry = build_default_tool_registry(workspace)
    register_research_markdown_tools(registry, workspace)
    if agent_config is not None and agent_config.name == "curator":
        workspace_root = getattr(getattr(workspace, "workspace", None), "root", None)
        if workspace_root:
            register_curated_blocker_registry_tool(
                registry,
                blocker_registry=PersistentBlockerRegistry(workspace_root),
                checkpoint_id=None,
            )
    for registrar in extra_registrars:
        registrar(registry)
    if agent_config is not None and agent_config.name in {"generator", "reviser"}:
        worker_rel = getattr(workspace, "worker_rel", None)
        workspace_root = getattr(getattr(workspace, "workspace", None), "root", None)
        if worker_rel and workspace_root:
            register_difficulty_declaration_tool(
                registry,
                declaration_path=workspace_root / worker_rel / "difficulty_declaration.md",
                role=agent_config.name,
            )
    if agent_config is not None and dispatcher is not None:
        register_agent_tool(
            registry,
            agent_config=agent_config,
            dispatcher=dispatcher,
            read_state_resolver=read_state_resolver,
        )
    return registry
