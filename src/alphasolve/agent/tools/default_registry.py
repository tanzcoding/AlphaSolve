from __future__ import annotations

import datetime
import json
import subprocess
from typing import Any

from ..shell import find_bash_path, has_bash, run_powershell_command
from ..workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES, WorkspaceLike
from .common import _format_grep_result, _format_list_result
from .filesystem import _format_list_dir_result
from .markdown import _markdown_index_progress_audit_hint, _markdown_read_review_hint
from .registry import ToolRegistry
from .types import ToolResult


def build_default_tool_registry(
    workspace: WorkspaceLike,
    *,
    bash_timeout_seconds: int = 120,
) -> ToolRegistry:
    """构造第二层默认 ToolRegistry，注册通用 coding agent 基础工具。

    第三层用法：传入 RoleWorkspaceAccess 实例（满足 WorkspaceLike），即可自动
    获得带角色权限检查的基础工具。Bash 在第二层不做命令白名单——具体安全策略
    由调用方通过 YAML 工具白名单或自定义 handler 控制。
    """
    registry = ToolRegistry()

    # Read
    def _run_read(args: dict[str, Any]) -> ToolResult:
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
        hint = _markdown_read_review_hint(str(args["path"]), result.output, result.message)
        index_audit_hint = _markdown_index_progress_audit_hint(workspace, str(args["path"]))
        if not result.output:
            return ToolResult(f"<system>{system}</system>")
        parts = [f"<system>{system}</system>", result.output]
        if hint:
            parts.append(hint)
        if index_audit_hint:
            parts.append(index_audit_hint)
        return ToolResult("\n".join(parts))

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
        handler=_run_read,
    )

    # Write
    registry.register(
        name="Write",
        description=(
            "Writes a file to the workspace.\n\n"
            "Usage:\n"
            "- Use `mode=\"overwrite\"` to replace the whole file.\n"
            "- Use `mode=\"append\"` to append content to the end of the file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to write."},
                "content": {"type": "string", "description": "The content to write to the file."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "default": "overwrite",
                    "description": "Whether to overwrite or append.",
                },
            },
            "required": ["path", "content"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                {"path": workspace.write_text(args["path"], args["content"], mode=str(args.get("mode", "overwrite")))},
                ensure_ascii=False,
            )
        ),
    )

    # Edit
    registry.register(
        name="Edit",
        description=(
            "Performs exact string replacements in files.\n\n"
            "Usage:\n"
            "- The edit will FAIL if old_str is not unique in the file.\n"
            "- Provide a larger string with more surrounding context to make it unique."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to modify."},
                "old_str": {"type": "string", "description": "The text to replace."},
                "new_str": {"type": "string", "description": "The text to replace it with (must be different from old_str)."},
            },
            "required": ["path", "old_str", "new_str"],
        },
        handler=lambda args: ToolResult(
            json.dumps({"path": workspace.edit(args["path"], args["old_str"], args["new_str"])}, ensure_ascii=False)
        ),
    )

    # MakeDir
    registry.register(
        name="MakeDir",
        description="Creates a directory in the workspace, including parent directories when needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(
            json.dumps({"path": workspace.make_dir(args["path"])}, ensure_ascii=False)
        ),
    )

    # Rename
    registry.register(
        name="Rename",
        description=(
            "Renames a file or directory in place.\n\n"
            "Usage:\n"
            "- Use this only when the item stays in the same directory and only its name changes.\n"
            "- `directory` is the parent directory that currently contains the item.\n"
            "- `old_name` and `new_name` must be plain names, not paths.\n"
            "- To move a file into another directory, use Move instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Parent directory containing the item to rename."},
                "old_name": {"type": "string", "description": "Current name only; no path separators."},
                "new_name": {"type": "string", "description": "New name only; no path separators."},
            },
            "required": ["directory", "old_name", "new_name"],
        },
        handler=lambda args: ToolResult(
            json.dumps(workspace.rename_item(args["directory"], args["old_name"], args["new_name"]), ensure_ascii=False)
        ),
    )

    # Move
    registry.register(
        name="Move",
        description=(
            "Moves a file into another directory while keeping the same file name.\n\n"
            "Usage:\n"
            "- `path` must be an existing file.\n"
            "- `destination_dir` must be an existing directory."
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
            json.dumps(workspace.move_file(args["path"], args["destination_dir"]), ensure_ascii=False)
        ),
    )

    # Delete
    registry.register(
        name="Delete",
        description=(
            "Deletes a file or empty directory.\n\n"
            "Usage:\n"
            "- Directories must already be empty; this tool will not recursively delete."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file or empty directory path to delete."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(
            json.dumps({"path": workspace.delete_path(args["path"])}, ensure_ascii=False)
        ),
    )

    # Glob
    registry.register(
        name="Glob",
        description=(
            "Fast file pattern matching tool. Supports glob patterns like ``**/*.md`` or ``src/**/*.py``, or ``*`` to list directory contents.\n\n"
            "Usage:\n"
            "- Returns plain text with one path per line.\n"
            "- In large workspaces, narrow `path` or `pattern` when you know the likely area."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against."},
                "path": {"type": "string", "description": "The directory to search in.", "default": "."},
                "max_results": {"type": "integer", "description": "Maximum results to return.", "default": 100},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(_format_list_result(
            "Glob results",
            workspace.glob(args["pattern"], path=args.get("path", "."), max_results=int(args.get("max_results", 100))),
            max_results=int(args.get("max_results", 100)),
        )),
    )

    # ListDir
    registry.register(
        name="ListDir",
        description=(
            "Lists files and directories under a workspace directory.\n\n"
            "Usage:\n"
            "- Returns plain text with one entry per line; directories end with `/`.\n"
            "- If the directory contains `index.md`, read `index.md` first before exploring other files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list.", "default": "."},
                "max_results": {"type": "integer", "description": "Maximum entries to return.", "default": 200},
            },
            "required": [],
        },
        handler=lambda args: ToolResult(_format_list_dir_result(
            args.get("path", "."),
            workspace.list_dir(args.get("path", "."), max_results=int(args.get("max_results", 200))),
            max_results=int(args.get("max_results", 200)),
        )),
    )

    # Grep
    registry.register(
        name="Grep",
        description=(
            "Searches readable text files under a specific file or directory.\n\n"
            "Usage:\n"
            "- Prefer Grep for exact symbol/string searches before reading many files.\n"
            "- Narrow `path` when you know the likely directory; broad searches can hit the result limit quickly.\n"
            "- Returns plain text in `path:line: text` format, optionally with context.\n"
            "- Use `context_lines` when the surrounding lines matter; keep it small for compact output.\n"
            "- `regex` defaults to true; set `regex=false` to force plain substring matching."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The text or regex pattern to search for."},
                "path": {"type": "string", "description": "File or directory to search in.", "default": "."},
                "regex": {"type": "boolean", "description": "Treat pattern as regex (default true).", "default": True},
                "max_results": {"type": "integer", "description": "Maximum results to return.", "default": 50},
                "context_lines": {"type": "integer", "description": "Lines of context around each match. Set 0 to disable.", "default": 0},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(_format_grep_result(
            workspace.grep(
                args["pattern"],
                path=args.get("path", "."),
                regex=bool(args.get("regex", True)),
                max_results=int(args.get("max_results", 50)),
                context_lines=int(args.get("context_lines", 0)),
            ),
            pattern=args["pattern"],
            path=args.get("path", "."),
            max_results=int(args.get("max_results", 50)),
        )),
    )

    # GetCurrentTime
    registry.register(
        name="GetCurrentTime",
        description="Returns the current date and time in ISO 8601 format.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda _args: ToolResult(
            json.dumps({"datetime": datetime.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False)
        ),
    )

    # Bash / Shell (按平台二选一)
    if has_bash():
        def _run_bash(args: dict[str, Any]) -> ToolResult:
            command = str(args.get("command") or "")
            if not command.strip():
                return ToolResult("[error] empty command", is_error=True)
            try:
                bash_path = find_bash_path()
                if bash_path is None:
                    return ToolResult("[error] bash not found", is_error=True)
                result = subprocess.run(
                    [str(bash_path), "-lc", command],
                    cwd=str(workspace.root),
                    text=True,
                    capture_output=True,
                    timeout=bash_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(f"[error] command timed out after {bash_timeout_seconds} seconds", is_error=True)
            parts = [f"[exit_code]\n{result.returncode}"]
            if result.stdout:
                parts.append(f"[stdout]\n{result.stdout}")
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            return ToolResult("\n".join(parts), is_error=result.returncode != 0)

        registry.register(
            name="Bash",
            description="Executes a bash command at the workspace root.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute."},
                },
                "required": ["command"],
            },
            handler=_run_bash,
        )
    else:
        def _run_shell(args: dict[str, Any]) -> ToolResult:
            command = str(args.get("command") or "")
            if not command.strip():
                return ToolResult("[error] empty command", is_error=True)
            try:
                result = run_powershell_command(
                    command,
                    cwd=str(workspace.root),
                    timeout=bash_timeout_seconds,
                )
            except FileNotFoundError as exc:
                return ToolResult(f"[error] {exc}", is_error=True)
            except subprocess.TimeoutExpired:
                return ToolResult(f"[error] command timed out after {bash_timeout_seconds} seconds", is_error=True)
            parts = [f"[exit_code]\n{result.returncode}"]
            if result.stdout:
                parts.append(f"[stdout]\n{result.stdout}")
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            return ToolResult("\n".join(parts), is_error=result.returncode != 0)

        registry.register(
            name="Shell",
            description=(
                "Executes a PowerShell command at the workspace root.\n\n"
                "IMPORTANT: Prefer dedicated tools for file operations:\n"
                "- Directory listing: Use ListDir\n"
                "- File search: Use Glob\n"
                "- Content search: Use Grep\n"
                "- Read files: Use Read\n"
                "- Edit files: Use Edit\n"
                "- Write files: Use Write"
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
