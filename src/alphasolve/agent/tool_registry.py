from __future__ import annotations

import json
import re
import shlex
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .config import GeneralAgentConfig
from .workspace import READ_PAGE_DEFAULT_LINES, READ_PAGE_MAX_LINES, Workspace
from alphasolve.utils.shell import has_bash, run_bash_command, run_powershell_command


ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


class SubagentDispatcher(Protocol):
    """第二层 Agent 工具的最小调度器协议。

    Why: 第二层不知道也不应该知道 reasoning/compute/numerical 等业务
    subagent 类型；这套方法让第三层 SubagentService 把"业务上的
    subagent 角色"翻译成"第二层认识的调度协议"。
    """
    def available_types(self) -> list[str]: ...
    def describe_type(self, agent_type: str) -> str: ...
    def call(self, agent_type: str, description: str, prompt: str, *, depth: int = ...) -> str: ...


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    stop_agent: bool = False
    stop_answer: str | None = None


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )

    def tool_defs(
        self,
        enabled: tuple[str, ...] | list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list["ToolDef"]:
        from alphasolve.llm.types import ToolDef
        names = list(enabled) if enabled is not None else list(self._tools)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise KeyError(f"unknown tools: {missing}")
        constraints = tool_parameters or {}
        out: list[ToolDef] = []
        for name in names:
            tool = self._tools[name]
            params = deepcopy(tool.parameters)
            constraint = constraints.get(name)
            if constraint:
                _apply_parameter_constraints(params, constraint)
            out.append(ToolDef(name=tool.name, description=tool.description, parameters=params))
        return out

    def registered_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def execute(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        enabled: list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ToolResult:
        if enabled is not None and name not in enabled:
            return ToolResult(json.dumps({"error": f"tool is not enabled for this agent: {name}"}, ensure_ascii=False), is_error=True)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False), is_error=True)
        constraints = (tool_parameters or {}).get(name) or {}
        effective_args = _apply_argument_defaults(args, tool.parameters, constraints)
        error = _validate_tool_arguments(name, effective_args, constraints)
        if error:
            return ToolResult(json.dumps({"error": error}, ensure_ascii=False), is_error=True)
        try:
            return tool.handler(effective_args)
        except Exception as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)


def _apply_parameter_constraints(parameters: dict[str, Any], constraints: Mapping[str, Any]) -> None:
    properties = parameters.setdefault("properties", {})
    if not isinstance(properties, dict):
        return
    for param_name, constraint in constraints.items():
        if not isinstance(constraint, Mapping):
            continue
        prop = properties.setdefault(str(param_name), {})
        if isinstance(prop, dict):
            prop.update(dict(constraint))


def _validate_tool_arguments(tool_name: str, args: Mapping[str, Any], constraints: Mapping[str, Any]) -> str | None:
    for param_name, constraint in constraints.items():
        if param_name not in args:
            continue
        if not isinstance(constraint, Mapping):
            continue
        error = _validate_value(args[param_name], constraint, path=f"{tool_name}.{param_name}")
        if error:
            return error
    return None


def _apply_argument_defaults(
    args: Mapping[str, Any],
    parameters: Mapping[str, Any],
    constraints: Mapping[str, Any],
) -> dict[str, Any]:
    out = dict(args)
    properties = parameters.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    names = set(properties) | set(constraints)
    for name in names:
        key = str(name)
        if key in out:
            continue
        merged: dict[str, Any] = {}
        base = properties.get(key)
        if isinstance(base, Mapping):
            merged.update(dict(base))
        constraint = constraints.get(key)
        if isinstance(constraint, Mapping):
            merged.update(dict(constraint))
        if "default" in merged:
            out[key] = deepcopy(merged["default"])
    return out


def _validate_value(value: Any, schema: Mapping[str, Any], *, path: str) -> str | None:
    if "const" in schema and value != schema["const"]:
        return f"{path} must be {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {list(schema['enum'])!r}"

    schema_type = schema.get("type")
    if schema_type:
        allowed = schema_type if isinstance(schema_type, list) else [schema_type]
        if not any(_matches_json_type(value, str(item)) for item in allowed):
            return f"{path} must have type {allowed!r}"

    for key, check in (
        ("minimum", lambda current, bound: current >= bound),
        ("maximum", lambda current, bound: current <= bound),
        ("exclusiveMinimum", lambda current, bound: current > bound),
        ("exclusiveMaximum", lambda current, bound: current < bound),
    ):
        if key in schema:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return f"{path} must be numeric to satisfy {key}"
            if not check(value, schema[key]):
                return f"{path} violates {key}={schema[key]!r}"

    if "minLength" in schema:
        if not isinstance(value, str) or len(value) < int(schema["minLength"]):
            return f"{path} violates minLength={schema['minLength']!r}"
    if "maxLength" in schema:
        if not isinstance(value, str) or len(value) > int(schema["maxLength"]):
            return f"{path} violates maxLength={schema['maxLength']!r}"
    if "pattern" in schema:
        if not isinstance(value, str) or re.search(str(schema["pattern"]), value) is None:
            return f"{path} must match pattern {schema['pattern']!r}"
    return None


def _matches_json_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "null":
        return value is None
    return True


def build_default_tool_registry(workspace: Workspace, *, bash_timeout_seconds: int = 120) -> ToolRegistry:
    registry = ToolRegistry()

    def run_read(args: dict[str, Any]) -> ToolResult:
        try:
            result = workspace.read_text_page(
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

    registry.register(
        name="Write",
        description="Writes a file to the local filesystem inside the workspace, creating parent directories if needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to write."},
                "content": {"type": "string", "description": "The content to write to the file."},
            },
            "required": ["path", "content"],
        },
        handler=lambda args: ToolResult(f"wrote {workspace.write_text(args['path'], args['content'])}"),
    )

    registry.register(
        name="Glob",
        description="Fast file pattern matching tool. Supports glob patterns like ``**/*.py`` or ``*`` to list directory contents.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against."},
                "path": {"type": "string", "default": ".", "description": "The directory to search in."},
                "max_results": {"type": "integer", "default": 100, "description": "Maximum results to return."},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                workspace.search_files(
                    args["pattern"],
                    path=args.get("path", "."),
                    max_results=int(args.get("max_results", 100)),
                ),
                ensure_ascii=False,
            )
        ),
    )

    if has_bash():
        def run_bash(args: dict[str, Any]) -> ToolResult:
            command = str(args.get("command") or "")
            if not command.strip():
                return ToolResult("[error] empty command", is_error=True)

            try:
                shlex.split(command)
            except ValueError:
                return ToolResult("[error] malformed command; could not parse", is_error=True)
            try:
                result = run_bash_command(
                    command,
                    cwd=str(workspace.root),
                    timeout=bash_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return ToolResult("[error] command timed out after 30 seconds", is_error=True)
            parts = [f"[exit_code]\n{result.returncode}"]
            if result.stdout:
                parts.append(f"[stdout]\n{result.stdout}")
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            return ToolResult("\n".join(parts), is_error=result.returncode != 0)

        registry.register(
            name="Bash",
            description="Executes a given bash command with the workspace root as the working directory.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
            handler=run_bash,
        )
    else:
        # Windows without Git Bash — provide PowerShell-backed shell tool.
        def run_shell(args: dict[str, Any]) -> ToolResult:
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
                return ToolResult("[error] command timed out after 30 seconds", is_error=True)
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
                "Git Bash is not available on this system.\n\n"
                "Prefer dedicated tools for file operations: "
                "Glob (NOT Get-ChildItem -Recurse), Read (NOT Get-Content), "
                "Write (NOT Set-Content/Out-File), Edit.\n\n"
                "Common aliases: ls (Get-ChildItem), cd (Set-Location), cat (Get-Content), rm (Remove-Item)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
            handler=run_shell,
        )

    return registry


def register_agent_tool(
    registry: ToolRegistry,
    *,
    agent_config: GeneralAgentConfig,
    dispatcher: SubagentDispatcher,
    depth: int = 0,
) -> None:
    """Register the Agent tool with a description and enum scoped to this agent's permissions.

    第三层（或其他实现）通过提供 SubagentDispatcher 实例注入业务 subagent 类型清单。
    """
    if "Agent" not in agent_config.tools:
        return
    agent_tool_params = agent_config.tool_parameters.get("Agent", {})
    type_constraint = agent_tool_params.get("type", {})
    allowed_types = type_constraint.get("enum", dispatcher.available_types())
    known = set(dispatcher.available_types())
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
        when = dispatcher.describe_type(stype)
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

    def _handler(args: dict[str, Any]) -> ToolResult:
        agent_type = str(args.get("type") or "")
        description = str(args.get("description") or "")
        prompt = str(args.get("prompt") or "")
        if not prompt.strip():
            return ToolResult("ERROR: prompt must not be empty", is_error=True)
        try:
            text = dispatcher.call(agent_type, description, prompt, depth=depth)
            return ToolResult(text)
        except Exception as exc:
            return ToolResult(f"ERROR: {exc}", is_error=True)

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
        handler=_handler,
    )
