"""Workflow 层的工具 registry 工厂：基础工具集（Read/Write/...）+ Agent 工具注册器。"""
from __future__ import annotations

import datetime
import json
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.agent import GeneralAgentConfig, ToolRegistry, ToolResult
from alphasolve.agent.workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES
from alphasolve.utils.shell import find_bash_path, has_bash, run_powershell_command

from .workspace_access import RoleWorkspaceAccess

if TYPE_CHECKING:
    from .subagent_service import SubagentService


def build_workspace_tool_registry(
    access: RoleWorkspaceAccess,
    *,
    allow_write: bool = False,
    allow_manage: bool = False,
    allow_delete: bool = False,
    subagent_service: "SubagentService | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    def run_read(args: dict[str, Any]) -> ToolResult:
        try:
            result = access.read_text_page(
                args["path"],
                line_offset=int(args.get("line_offset", 1)),
                n_lines=int(args.get("n_lines", READ_PAGE_DEFAULT_LINES)),
                read_all=bool(args.get("read_all", False)),
            )
        except Exception as exc:
            return ToolResult(f"<system>ERROR: {exc}</system>", is_error=True)
        return ToolResult(result.to_tool_content())

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
            f"- Without `read_all`, the maximum `n_lines` value is {READ_PAGE_MAX_LINES}.\n"
            "- Results respect this agent's workspace access restrictions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to read (must be absolute or workspace-relative)."},
                "line_offset": {
                    "type": "integer",
                    "default": 1,
                    "minimum": 1,
                    "description": (
                        "The line number to start reading from. "
                        "By default read from the beginning of the file. "
                        "Set this when the file is too large to read at once."
                    ),
                },
                "n_lines": {
                    "type": "integer",
                    "default": READ_PAGE_DEFAULT_LINES,
                    "minimum": 1,
                    "maximum": READ_PAGE_MAX_LINES,
                    "description": (
                        f"How many lines to return in this Read call. Defaults to {READ_PAGE_DEFAULT_LINES}. "
                        f"At most {READ_PAGE_MAX_LINES} unless `read_all` is true."
                    ),
                },
                "read_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, ignore n_lines and return every line from line_offset through the end of the file.",
                },
            },
            "required": ["path"],
        },
        handler=run_read,
    )
    if allow_write:
        registry.register(
            name="Write",
            description="Writes a file within this agent's allowed write area.\n\nUsage:\n- Use `mode=\"overwrite\"` to replace the whole file.\n- Use `mode=\"append\"` to append content to the end of the file.\n- If this is an existing file, you MUST use the Read tool first to read the file's contents.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to write."},
                    "content": {"type": "string", "description": "The content to write to the file."},
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "default": "overwrite",
                        "description": "Whether to overwrite the whole file or append to the end.",
                    },
                },
                "required": ["path", "content"],
            },
            handler=lambda args: ToolResult(
                json.dumps(
                    {
                        "path": access.write_text(
                            args["path"],
                            args["content"],
                            mode=str(args.get("mode", "overwrite")),
                        )
                    },
                    ensure_ascii=False,
                )
            ),
        )
        registry.register(
            name="Edit",
            description=(
                "Performs exact string replacements in files.\n\n"
                "Usage:\n"
                "- The edit will FAIL if old_string is not unique in the file.\n"
                "- Provide a larger string with more surrounding context to make it unique."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to modify."},
                    "old_str": {"type": "string", "description": "The text to replace."},
                    "new_str": {"type": "string", "description": "The text to replace it with (must be different from old_string)."},
                },
                "required": ["path", "old_str", "new_str"],
            },
            handler=lambda args: ToolResult(
                json.dumps({"path": access.edit(args["path"], args["old_str"], args["new_str"])}, ensure_ascii=False)
            ),
        )
    if allow_manage:
        registry.register(
            name="MakeDir",
            description="Creates a directory within the writable workspace area, including parent directories when needed.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to create."},
                },
                "required": ["path"],
            },
            handler=lambda args: ToolResult(
                json.dumps({"path": access.make_dir(args["path"])}, ensure_ascii=False)
            ),
        )
        registry.register(
            name="Rename",
            description=(
                "Renames a file or directory in place within the writable workspace area.\n\n"
                "Usage:\n"
                "- Use this only when the item stays in the same directory and only its name changes.\n"
                "- `directory` is the parent directory that currently contains the item.\n"
                "- `old_name` and `new_name` must be plain names, not paths, and must not contain `/` or `\\`.\n"
                "- To move a file into another directory, use Move instead.\n"
                "- Some agents are configured to preserve Markdown file names; in those sessions, renaming `.md` files fails and Move must keep the same file name.\n"
                "- This tool fails if the target path already exists.\n\n"
                "Examples:\n"
                "- Rename a folder: directory=\"verified_propositions\", old_name=\"bootstrap-A\", "
                "new_name=\"bootstrap-attempt-A\".\n"
                "- Rename a knowledge page: directory=\"knowledge/fourier\", old_name=\"cutoff.md\", "
                "new_name=\"frequency-cutoff.md\"."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Parent directory containing the item to rename."},
                    "old_name": {"type": "string", "description": "Current file or directory name only; no path separators."},
                    "new_name": {"type": "string", "description": "New file or directory name only; no path separators."},
                },
                "required": ["directory", "old_name", "new_name"],
            },
            handler=lambda args: ToolResult(
                json.dumps(access.rename_item(args["directory"], args["old_name"], args["new_name"]), ensure_ascii=False)
            ),
        )
        registry.register(
            name="Move",
            description=(
                "Moves a file into another directory within the writable workspace area while keeping the same file name.\n\n"
                "Usage:\n"
                "- Use this when reorganizing files into topic folders.\n"
                "- `path` must be an existing file.\n"
                "- `destination_dir` must be an existing directory.\n"
                "- Move never renames the file; it always keeps the source file name.\n"
                "- When moving files inside verified_propositions/, Move automatically updates matching "
                "`\\ref{old-path}` references across verified_propositions/ to the new backslash-separated path."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Existing file path to move."},
                    "destination_dir": {"type": "string", "description": "Existing destination directory."},
                },
                "required": ["path", "destination_dir"],
            },
            handler=lambda args: ToolResult(
                json.dumps(access.move_file(args["path"], args["destination_dir"]), ensure_ascii=False)
            ),
        )
        if allow_write and allow_delete:
            registry.register(
                name="Delete",
                description=(
                    "Deletes a file or empty directory from the writable workspace area.\n\n"
                    "Usage:\n"
                    "- Only delete files after useful content has been consolidated elsewhere.\n"
                    "- Directories must already be empty; this tool will not recursively delete directory contents."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "The file or empty directory path to delete."},
                    },
                    "required": ["path"],
                },
                handler=lambda args: ToolResult(
                    json.dumps({"path": access.delete_path(args["path"])}, ensure_ascii=False)
                ),
            )

    registry.register(
        name="Glob",
        description="Fast file pattern matching tool that works with any codebase size. Supports glob patterns like ``**/*.md`` or ``src/**/*.py``, or ``*`` to list directory contents.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against."},
                "path": {"type": "string", "description": "The directory to search in. Defaults to this agent's configured search root.", "default": "."},
                "max_results": {"type": "integer", "description": "Maximum results to return.", "default": 100},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                access.glob(
                    args["pattern"],
                    path=args.get("path", "."),
                    max_results=int(args.get("max_results", 100)),
                ),
                ensure_ascii=False,
            )
        ),
    )
    registry.register(
        name="ListDir",
        description=(
            "Lists files and directories under a workspace directory.\n\n"
            "Usage:\n"
            "- If the directory contains `index.md`, read `index.md` first before exploring other files.\n"
            "- The result respects this agent's workspace access restrictions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list. Defaults to this agent's configured read root.", "default": "."},
                "max_results": {"type": "integer", "description": "Maximum entries to return.", "default": 200},
            },
            "required": [],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                access.list_dir(
                    args.get("path", "."),
                    max_results=int(args.get("max_results", 200)),
                ),
                ensure_ascii=False,
            )
        ),
    )
    registry.register(
        name="Grep",
        description="Searches readable text files under a specific file or directory.\n\nUsage:\n- Prefer Grep for exact symbol/string searches.\n- `regex` defaults to true; set `regex=false` to force plain substring matching.\n- Results respect this agent's workspace access restrictions.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The text or regular expression pattern to search for in file contents."},
                "path": {"type": "string", "description": "File or directory to search in. Defaults to this agent's configured search root.", "default": "."},
                "regex": {"type": "boolean", "description": "Treat pattern as a regex (default: true; set false for substring matching).", "default": True},
                "max_results": {"type": "integer", "description": "Maximum results to return.", "default": 50},
                "context_lines": {"type": "integer", "description": "Number of context lines around each match.", "default": 0},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                access.grep(
                    args["pattern"],
                    path=args.get("path", "."),
                    regex=bool(args.get("regex", True)),
                    max_results=int(args.get("max_results", 50)),
                    context_lines=int(args.get("context_lines", 0)),
                ),
                ensure_ascii=False,
            )
        ),
    )

    # Agent tool is registered per-agent via register_agent_tool(), not here,
    # because the set of allowed subagent types varies by agent config.

    registry.register(
        name="GetCurrentTime",
        description="Returns the current date and time in ISO 8601 format.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=lambda _args: ToolResult(
            json.dumps({"datetime": datetime.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False)
        ),
    )

    def _run_bash(args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "")
        if not command.strip():
            return ToolResult("[error] empty command", is_error=True)
        try:
            argv = shlex.split(command)
        except ValueError:
            return ToolResult("[error] malformed command; could not parse", is_error=True)
        if not argv:
            return ToolResult("[error] empty command", is_error=True)
        # Safety: only allow ls
        cmd_name = Path(argv[0]).name
        if cmd_name != "ls":
            return ToolResult("[error] only `ls` is allowed for safety", is_error=True)
        try:
            bash_path = find_bash_path()
            if bash_path is None:
                return ToolResult("[error] bash not found", is_error=True)
            result = subprocess.run(
                [str(bash_path), "-c", 'exec ls "$@"', "alphasolve-ls", *argv[1:]],
                cwd=str(access.workspace.root),
                text=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return ToolResult("[error] command timed out after 30 seconds", is_error=True)
        parts = [f"[exit_code]\n{result.returncode}"]
        if result.stdout:
            parts.append(f"[stdout]\n{result.stdout}")
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        return ToolResult("\n".join(parts), is_error=result.returncode != 0)


    if has_bash():
        registry.register(
            name="Bash",
            description="Executes a bash command at the workspace root. Only `ls` is currently allowed for safety. "
            "Prefer ListDir for directory listings and Glob/Grep/Read for file inspection.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute. Only `ls` is permitted."},
                },
                "required": ["command"],
            },
            handler=_run_bash,
        )
    else:
        # Windows without Git Bash - provide a PowerShell-backed shell tool.
        def _run_shell(args: dict[str, Any]) -> ToolResult:
            command = str(args.get("command") or "")
            if not command.strip():
                return ToolResult("[error] empty command", is_error=True)
            try:
                result = run_powershell_command(
                    command,
                    cwd=str(access.workspace.root),
                    timeout=60,
                )
            except FileNotFoundError as exc:
                return ToolResult(f"[error] {exc}", is_error=True)
            except subprocess.TimeoutExpired:
                return ToolResult("[error] command timed out after 60 seconds", is_error=True)
            parts = [f"[exit_code]\n{result.returncode}"]
            if result.stdout:
                parts.append(f"[stdout]\n{result.stdout}")
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            return ToolResult("\n".join(parts), is_error=result.returncode != 0)

        registry.register(
            name="Shell",
            description=(
                "Executes a PowerShell command at the workspace root. "
                "Each call is a fresh shell; state does not persist.\n\n"
                "Git Bash is not available on this system, so this is the primary shell tool.\n\n"
                "IMPORTANT: Prefer dedicated tools for file operations:\n"
                "- Directory listing: Use ListDir (NOT ls/Get-ChildItem)\n"
                "- File search: Use Glob (NOT Get-ChildItem -Recurse)\n"
                "- Content search: Use Grep (NOT Select-String)\n"
                "- Read files: Use Read (NOT Get-Content)\n"
                "- Edit files: Use Edit\n"
                "- Write files: Use Write (NOT Set-Content/Out-File)\n\n"
                "Common aliases: ls (Get-ChildItem), cd (Set-Location), cat (Get-Content), rm (Remove-Item)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The PowerShell command to execute."},
                },
                "required": ["command"],
            },
            handler=_run_shell,
        )

    return registry


def register_agent_tool(
    registry: ToolRegistry,
    *,
    agent_config: GeneralAgentConfig,
    subagent_service: "SubagentService",
    depth: int = 0,
) -> None:
    """Register the Agent tool with a description and enum scoped to this agent's permissions."""
    if "Agent" not in agent_config.tools:
        return
    agent_tool_params = agent_config.tool_parameters.get("Agent", {})
    type_constraint = agent_tool_params.get("type", {})
    allowed_types = type_constraint.get("enum", subagent_service.available_types())
    known = set(subagent_service.available_types())
    allowed_types = [t for t in allowed_types if t in known]
    if not allowed_types:
        return
    lines = [
        "Launch a new specialized agent and return its final report.",
        "",
        "The launched agent runs in a separate session. It does not see this conversation unless you include the needed context in `prompt`.",
        "",
        "Available agent types:",
    ]
    for stype in allowed_types:
        config = subagent_service.suite.subagents.get(stype)
        when = config.when_to_use if config and config.when_to_use else stype
        lines.append(f"- {stype}: {when}")
    lines.extend([
        "",
        "## Writing the prompt",
        "",
        "- State the exact mathematical claim, definitions, assumptions, and what needs to be proved or checked.",
        "- Include relevant context: what's already known, what approaches have been tried, which files to reference, and why this task matters.",
        "- Give enough context that the agent can make judgment calls rather than just following a narrow instruction.",
        "- Keep the task self-contained and bounded to the agent's scope.",
    ])
    registry.register(
        name="Agent",
        description="\n".join(lines),
        parameters={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": allowed_types, "description": "The type of specialized agent to use for this task."},
                "description": {"type": "string", "description": "A short (3-5 word) description of the task."},
                "prompt": {"type": "string", "description": "The task for the agent to perform."},
            },
            "required": ["type", "description", "prompt"],
        },
        handler=lambda args: subagent_service.call_tool(args, depth=depth),
    )
