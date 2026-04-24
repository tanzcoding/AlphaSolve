from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .workspace import Workspace


ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
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

    def openai_tools(self, enabled: list[str] | None = None) -> list[dict[str, Any]]:
        names = enabled if enabled is not None else list(self._tools)
        missing = [name for name in names if name not in self._tools]
        if missing:
            raise KeyError(f"unknown tools: {missing}")
        return [self._tools[name].to_openai_tool() for name in names]

    def registered_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def execute(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False), is_error=True)
        try:
            return tool.handler(dict(args))
        except Exception as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)


def build_default_tool_registry(workspace: Workspace, *, bash_timeout_seconds: int = 120) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        name="read_file",
        description="Read a UTF-8 text file inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(workspace.read_text(args["path"], max_chars=int(args.get("max_chars", 20000)))),
    )

    registry.register(
        name="write_file",
        description="Write a UTF-8 text file inside the workspace, creating parent directories if needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        handler=lambda args: ToolResult(f"wrote {workspace.write_text(args['path'], args['content'])}"),
    )

    registry.register(
        name="list_dir",
        description="List files and directories inside a workspace directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
            },
            "required": [],
        },
        handler=lambda args: ToolResult(json.dumps(workspace.list_dir(args.get("path", ".")), ensure_ascii=False)),
    )

    registry.register(
        name="search_files",
        description="Find files by glob pattern inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["pattern"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                workspace.search_files(
                    args["pattern"],
                    path=args.get("path", "."),
                    max_results=int(args.get("max_results", 50)),
                ),
                ensure_ascii=False,
            )
        ),
    )

    def run_bash(args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command") or "")
        if not command.strip():
            return ToolResult("[error] empty command", is_error=True)

        result = subprocess.run(
            command,
            cwd=str(workspace.root),
            shell=True,
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
        name="bash",
        description="Run a shell command with the workspace root as the working directory.",
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
