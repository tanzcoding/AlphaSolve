from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agents.general import GeneralAgentConfig, GeneralPurposeAgent, ToolRegistry, ToolResult, Workspace
from alphasolve.execution.runners import run_python, run_wolfram
from alphasolve.utils.shell import (
    has_bash,
    run_bash_command,
    run_powershell_command,
)

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway


ClientFactory = Callable[[GeneralAgentConfig], Any]


@dataclass
class RoleWorkspaceAccess:
    workspace: Workspace
    worker_rel: str | None = None
    deny_other_unverified: bool = False
    read_root_rel: str | None = None
    write_root_rel: str | None = None
    deny_read_rel: str | None = None  # deny reads under this subtree (used to block other verifier attempt dirs)
    deny_read_file_names: tuple[str, ...] = ()
    exact_write_rel: str | None = None
    single_proposition_file: bool = False
    allowed_extensions: tuple[str, ...] = (".md", ".py", ".lean")
    max_read_chars: int = 20000

    def __post_init__(self) -> None:
        self._locked_proposition_rel: str | None = None

    def read_text(self, path: str, *, max_chars: int | None = None) -> str:
        target = self._resolve_readable_file(path)
        text = target.read_text(encoding="utf-8")
        limit = self.max_read_chars if max_chars is None else int(max_chars)
        if limit > 0 and len(text) > limit:
            return text[:limit] + "\n[truncated]"
        return text

    def write_text(self, path: str, content: str) -> str:
        target = self._resolve_writable_file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return self._rel(target)

    def edit(self, path: str, old_str: str, new_str: str) -> str:
        target = self._resolve_writable_file(path)
        text = target.read_text(encoding="utf-8")
        if old_str not in text:
            raise ValueError(f"old_str not found in {path}")
        if text.count(old_str) > 1:
            raise ValueError(f"old_str matches multiple locations in {path}; make it more specific")
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return self._rel(target)

    def list_dir(self, path: str = ".", *, recursive: bool = False, max_results: int = 200) -> list[str]:
        target = self.workspace.resolve(path)
        self._ensure_under_read_root(target)
        self._ensure_not_other_worker_path(target)
        if not target.is_dir():
            raise ValueError(f"not a directory: {path}")

        out: list[str] = []
        if recursive:
            for current, dirs, files in os.walk(target):
                current_path = Path(current)
                dirs[:] = [
                    name
                    for name in dirs
                    if name not in {".git", "__pycache__", ".venv", "node_modules"}
                    and not self._is_other_worker_path(current_path / name)
                ]
                for name in sorted(dirs):
                    out.append(self._rel(current_path / name) + "/")
                    if len(out) >= max_results:
                        return out
                for name in sorted(files):
                    file_path = current_path / name
                    if not self._is_other_worker_path(file_path) and not self._is_denied_read_file(file_path):
                        out.append(self._rel(file_path))
                        if len(out) >= max_results:
                            return out
            return out

        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            if self._is_other_worker_path(child):
                continue
            if self._is_denied_read_file(child):
                continue
            out.append(self._rel(child) + ("/" if child.is_dir() else ""))
            if len(out) >= max_results:
                break
        return out

    def glob(self, pattern: str, *, path: str = ".", max_results: int = 100) -> list[str]:
        """Fast file pattern matching, e.g. ``*.md`` or ``**/*.py``."""
        target = self.workspace.resolve(path)
        self._ensure_under_read_root(target)
        self._ensure_not_other_worker_path(target)
        if not target.is_dir():
            raise ValueError(f"not a directory: {path}")

        out: list[str] = []
        recursive = "**" in pattern
        match_func = target.rglob if recursive else target.glob
        for child_path in sorted(match_func(pattern)):
            if self._is_other_worker_path(child_path):
                continue
            if self._is_denied_read_file(child_path):
                continue
            if child_path.is_file() and self._extension_allowed(child_path):
                out.append(self._rel(child_path))
            elif child_path.is_dir() and not child_path.name.startswith("."):
                out.append(self._rel(child_path) + "/")
            if len(out) >= max_results:
                break
        return out

    def grep(
        self,
        pattern: str,
        *,
        path: str = ".",
        regex: bool = False,
        max_results: int = 50,
        context_lines: int = 0,
    ) -> list[dict[str, Any]]:
        if not pattern:
            raise ValueError("pattern must not be empty")
        root = self.workspace.resolve(path)
        self._ensure_under_read_root(root)
        self._ensure_not_other_worker_path(root)
        if not root.exists():
            raise ValueError(f"path does not exist: {path}")

        files = [root] if root.is_file() else self._iter_text_files(root)
        out: list[dict[str, Any]] = []
        compiled = re.compile(pattern) if regex else None
        for file_path in files:
            if self._is_other_worker_path(file_path) or not self._extension_allowed(file_path):
                continue
            if self._is_denied_read_file(file_path):
                continue
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines, start=1):
                hit = bool(compiled.search(line)) if compiled else pattern in line
                if not hit:
                    continue
                start = max(1, index - int(context_lines))
                end = min(len(lines), index + int(context_lines))
                out.append(
                    {
                        "path": self._rel(file_path),
                        "line": index,
                        "text": line,
                        "context": "\n".join(
                            f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1)
                        ),
                    }
                )
                if len(out) >= max_results:
                    return out
        return out

    def _resolve_readable_file(self, path: str) -> Path:
        target = self.workspace.resolve(path)
        self._ensure_under_read_root(target)
        self._ensure_not_other_worker_path(target)
        if self._is_denied_read_file(target):
            raise ValueError(f"read access to {target.name} is denied for this agent")
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        if not self._extension_allowed(target):
            raise ValueError(f"file extension is not allowed: {path}")
        return target

    def _resolve_writable_file(self, path: str) -> Path:
        target = self.workspace.resolve(path)
        if self.exact_write_rel is not None:
            exact = self.workspace.resolve(self.exact_write_rel)
            if target != exact:
                raise ValueError(f"write_file can only rewrite {self.exact_write_rel}")
            return target

        if self.single_proposition_file:
            if self.worker_rel is None:
                raise ValueError("single proposition write requires worker_rel")
            worker_dir = self.workspace.resolve(self.worker_rel)
            proposition_path = worker_dir / "proposition.md"
            if target != proposition_path:
                raise ValueError("generator can only write proposition.md in its own directory")
            self._locked_proposition_rel = self._rel(proposition_path)
            return target

        if self.write_root_rel is None:
            raise ValueError("write_file is not enabled for this agent")
        self._ensure_under_root(target, self.write_root_rel, kind="write")
        return target

    def _iter_text_files(self, root: Path):
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if name not in {".git", "__pycache__", ".venv", "node_modules"}
                and not self._is_other_worker_path(current_path / name)
            ]
            for filename in files:
                file_path = current_path / filename
                if not self._is_denied_read_file(file_path):
                    yield file_path

    def _extension_allowed(self, path: Path) -> bool:
        return path.suffix.lower() in self.allowed_extensions

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace.root).as_posix()

    def _ensure_not_other_worker_path(self, path: Path) -> None:
        if self._is_other_worker_path(path):
            raise ValueError(f"access to other unverified proposition worker directories is denied: {self._rel(path)}")

    def _ensure_under_root(self, path: Path, root_rel: str, *, kind: str = "path") -> None:
        root = self.workspace.resolve(root_rel)
        if path != root and root not in path.parents:
            raise ValueError(f"{kind} path must stay under {root_rel}")

    def _ensure_under_read_root(self, path: Path) -> None:
        if self.read_root_rel is not None:
            self._ensure_under_root(path, self.read_root_rel, kind="read")
        if self.deny_read_rel is not None:
            deny_root = self.workspace.resolve(self.deny_read_rel)
            if path == deny_root or deny_root in path.parents:
                raise ValueError(f"read access to {self.deny_read_rel} is denied for this agent")

    def _is_other_worker_path(self, path: Path) -> bool:
        if not self.deny_other_unverified:
            return False
        rel = self._rel(path)
        prefix = "unverified_propositions/"
        if not (rel == "unverified_propositions" or rel.startswith(prefix)):
            return False
        if self.worker_rel is None:
            return True
        worker_rel = self.worker_rel.strip("/")
        return not (rel == worker_rel or rel.startswith(worker_rel + "/") or rel == "unverified_propositions")

    def _is_denied_read_file(self, path: Path) -> bool:
        return path.is_file() and path.name in set(self.deny_read_file_names)


def build_workspace_tool_registry(
    access: RoleWorkspaceAccess,
    *,
    allow_write: bool = False,
    subagent_service: "SubagentService | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        name="Read",
        description="Reads a file from the local filesystem. You can access any file directly by using this tool.\n\nUsage:\n- You can optionally specify a max_chars (especially handy for long files), but it's recommended to read the whole file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The path to the file to read (must be absolute or workspace-relative)."},
                "max_chars": {"type": "integer", "default": access.max_read_chars, "description": "Maximum characters to read."},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(
            access.read_text(args["path"], max_chars=int(args.get("max_chars", access.max_read_chars)))
        ),
    )
    if allow_write:
        registry.register(
            name="Write",
            description="Writes a file to the local filesystem.\n\nUsage:\n- This tool will overwrite the existing file if there is one at the provided path.\n- If this is an existing file, you MUST use the Read tool first to read the file's contents.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The path to the file to write."},
                    "content": {"type": "string", "description": "The content to write to the file."},
                },
                "required": ["path", "content"],
            },
            handler=lambda args: ToolResult(json.dumps({"path": access.write_text(args["path"], args["content"])}, ensure_ascii=False)),
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

    registry.register(
        name="Glob",
        description="Fast file pattern matching tool that works with any codebase size. Supports glob patterns like ``**/*.md`` or ``src/**/*.py``, or ``*`` to list directory contents.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against."},
                "path": {"type": "string", "description": "The directory to search in. Defaults to workspace root.", "default": "."},
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
        name="Grep",
        description="A powerful search tool built on ripgrep. Search specific text (in the pattern parameter) under a specific directory.\n\nUsage:\n- Prefer Grep for exact symbol/string searches. Whenever possible, use this instead of terminal grep/rg. This tool is faster and respects .gitignore.\n- Supports full regex syntax and file type filtering.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regular expression pattern to search for in file contents."},
                "path": {"type": "string", "description": "File or directory to search in (rg PATH). Defaults to current working directory.", "default": "."},
                "regex": {"type": "boolean", "description": "Treat pattern as a regex (default: substring match).", "default": False},
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
                    regex=bool(args.get("regex", False)),
                    max_results=int(args.get("max_results", 50)),
                    context_lines=int(args.get("context_lines", 0)),
                ),
                ensure_ascii=False,
            )
        ),
    )

    if subagent_service is not None:
        subagent_types = subagent_service.available_types()
        registry.register(
            name="Agent",
            description="Launch a new agent to handle complex, multi-step tasks autonomously.\n\nThe Agent tool launches specialized agents (subprocesses) that autonomously handle complex tasks. Each agent type has specific capabilities and tools available to it.\n\nAvailable agent types and their purposes:\n- compute_subagent: Concrete symbolic or numeric computation and verification.\n- numerical_experiment_subagent: Bounded exploration, branch checks, and local numerical experiments.\n- reasoning_subagent: Bounded proof and mathematical reasoning obligations.",
            parameters={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": subagent_types, "description": "The type of specialized agent to use for this task."},
                    "task": {"type": "string", "description": "The task for the agent to perform."},
                },
                "required": ["type", "task"],
            },
            handler=lambda args: subagent_service.call_tool(args),
        )

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
            result = run_bash_command(
                command,
                cwd=str(access.workspace.root),
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
            description="Executes a bash command at the workspace root. Only `ls` is allowed for safety. "
            "Use this to verify directory contents (e.g. `ls verified_propositions/`) when Glob returns an empty or unexpected result.",
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
        # Windows without Git Bash — provide a PowerShell-backed shell tool.
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
        digest_queue: "Any | None" = None,
        digest_context_provider: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        log_session: "Any | None" = None,
    ) -> None:
        self.suite = suite
        self.client_factory = client_factory
        self.max_depth = max(0, int(max_depth))
        self.file_access_factory = file_access_factory
        self.file_allow_write = bool(file_allow_write)
        self.execution_gateway = execution_gateway
        self.session_prefix = session_prefix
        self.digest_queue = digest_queue
        self.digest_context_provider = digest_context_provider
        self.log_session = log_session

    def available_types(self) -> list[str]:
        return sorted(self.suite.subagents)

    def call_tool(self, args: dict[str, Any], *, depth: int = 0) -> ToolResult:
        agent_type = str(args.get("type") or "")
        task = str(args.get("task") or "")
        if not task.strip():
            return ToolResult(json.dumps({"error": "task must not be empty"}, ensure_ascii=False), is_error=True)
        try:
            result = self.call(agent_type, task, depth=depth)
        except Exception as exc:
            return ToolResult(json.dumps({"error": str(exc)}, ensure_ascii=False), is_error=True)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    def call(self, agent_type: str, task: str, *, depth: int = 0) -> dict[str, Any]:
        config = self.suite.subagents.get(agent_type)
        if config is None:
            allowed = ", ".join(self.available_types())
            raise ValueError(f"unknown subagent type: {agent_type}. Allowed types: {allowed}")
        session_id = self._make_session_id(agent_type=agent_type, depth=depth)
        registry = self._build_subagent_registry(depth=depth, session_id=session_id)
        enabled_tools = list(config.tools)
        if self.file_access_factory is not None:
            for name in ("Read", "Glob", "Grep"):
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
                if name not in {"Read", "Write", "Edit", "Glob", "Grep"}
            ]
        if depth >= self.max_depth and "Agent" in enabled_tools:
            enabled_tools = [name for name in enabled_tools if name != "Agent"]
        if enabled_tools != list(config.tools):
            config = GeneralAgentConfig(
                name=config.name,
                system_prompt=config.system_prompt,
                tools=enabled_tools,
                tool_parameters=config.tool_parameters,
                max_turns=config.max_turns,
                model_config=config.model_config,
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
            )
            result = agent.run(task)
        finally:
            if subagent_sink is not None:
                subagent_sink.close()
        out = {
            "type": agent_type,
            "session_id": session_id,
            "final_answer": result.final_answer,
            "turns": result.turns,
            "trace": result.trace,
        }
        # Submit trace to digest queue unless we ARE the digest agent (avoid recursion)
        if self.digest_queue is not None and not self.session_prefix.startswith("knowledge_digest") and not self.session_prefix.startswith("orchestrator"):
            from .knowledge_digest import DigestTask
            caller_context = None
            if self.digest_context_provider is not None:
                try:
                    caller_context = self.digest_context_provider(
                        {
                            "agent_type": agent_type,
                            "session_id": session_id,
                            "task": task,
                            "result": out,
                        }
                    )
                except Exception as exc:
                    caller_context = {"digest_context_error": str(exc)}
            self.digest_queue.submit(DigestTask(
                trace_segment=result.trace,
                source_label=f"{self.session_prefix}/{agent_type}",
                caller_context=caller_context,
            ))
        return out

    def _build_subagent_registry(self, *, depth: int, session_id: str) -> ToolRegistry:
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
        if self.file_access_factory is not None:
            access = self.file_access_factory()
            file_registry = build_workspace_tool_registry(access, allow_write=self.file_allow_write)
            for tool in file_registry.registered_tools():
                registry.register(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    handler=tool.handler,
                )
        if depth < self.max_depth:
            registry.register(
                name="Agent",
                description="Launch a new agent to handle complex, multi-step tasks autonomously. Recursive depth is bounded.",
                parameters={
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": self.available_types(), "description": "The type of specialized agent to use."},
                        "task": {"type": "string", "description": "The task for the agent to perform."},
                    },
                    "required": ["type", "task"],
                },
                handler=lambda args: self.call_tool(args, depth=depth + 1),
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
