from __future__ import annotations

import json
import re
import shlex
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .workspace import Workspace


ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


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

    def to_openai_tool(self, parameter_constraints: Mapping[str, Any] | None = None) -> dict[str, Any]:
        parameters = deepcopy(self.parameters)
        if parameter_constraints:
            _apply_parameter_constraints(parameters, parameter_constraints)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


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

    def openai_tools(
        self,
        enabled: list[str] | None = None,
        tool_parameters: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        names = enabled if enabled is not None else list(self._tools)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise KeyError(f"unknown tools: {missing}")
        constraints = tool_parameters or {}
        return [self._tools[name].to_openai_tool(constraints.get(name)) for name in names]

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

    registry.register(
        name="Read",
        description="Reads a file from the local filesystem inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to read."},
                "max_chars": {"type": "integer", "default": 20000, "description": "Maximum characters to read."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(workspace.read_text(args["path"], max_chars=int(args.get("max_chars", 20000)))),
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

    def run_bash(args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "")
        if not command.strip():
            return ToolResult("[error] empty command", is_error=True)

        try:
            argv = shlex.split(command)
        except ValueError:
            return ToolResult("[error] malformed command; could not parse", is_error=True)
        result = subprocess.run(
            argv,
            cwd=str(workspace.root),
            shell=False,
            text=True,
            capture_output=True,
            timeout=bash_timeout_seconds,
        )
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

    return registry
