from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from alphasolve.agents.general import GeneralAgentConfig, GeneralPurposeAgent, ToolRegistry, ToolResult, Workspace
from alphasolve.execution.runners import run_python, run_wolfram

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
    exact_write_rel: str | None = None
    single_lemma_file: bool = False
    allowed_extensions: tuple[str, ...] = (".md", ".py", ".lean")
    max_read_chars: int = 20000

    def __post_init__(self) -> None:
        self._locked_lemma_rel: str | None = None

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

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
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
                    if not self._is_other_worker_path(file_path):
                        out.append(self._rel(file_path))
                        if len(out) >= max_results:
                            return out
            return out

        for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            if self._is_other_worker_path(child):
                continue
            out.append(self._rel(child) + ("/" if child.is_dir() else ""))
            if len(out) >= max_results:
                break
        return out

    def search_files(self, fragment: str, *, path: str = ".", max_results: int = 50) -> list[str]:
        if not fragment:
            raise ValueError("fragment must not be empty")
        root = self.workspace.resolve(path)
        self._ensure_under_read_root(root)
        self._ensure_not_other_worker_path(root)
        if not root.is_dir():
            raise ValueError(f"not a directory: {path}")

        needle = fragment.lower()
        matches: list[str] = []
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
                if self._is_other_worker_path(file_path):
                    continue
                if needle in filename.lower() and self._extension_allowed(file_path):
                    matches.append(self._rel(file_path))
                    if len(matches) >= max_results:
                        return matches
        return matches

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

        if self.single_lemma_file:
            if self.worker_rel is None:
                raise ValueError("single lemma write requires worker_rel")
            worker_dir = self.workspace.resolve(self.worker_rel)
            if target.parent != worker_dir:
                raise ValueError("generator can only write one markdown file directly in its own lemma directory")
            if target.name == "review.md" or target.suffix.lower() != ".md":
                raise ValueError("generator output must be a lemma markdown file")
            rel = self._rel(target)
            if self._locked_lemma_rel is None:
                self._locked_lemma_rel = rel
            elif self._locked_lemma_rel != rel:
                raise ValueError(f"generator can only rewrite {self._locked_lemma_rel}")
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
                yield current_path / filename

    def _extension_allowed(self, path: Path) -> bool:
        return path.suffix.lower() in self.allowed_extensions

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace.root).as_posix()

    def _ensure_not_other_worker_path(self, path: Path) -> None:
        if self._is_other_worker_path(path):
            raise ValueError(f"access to other unverified lemma worker directories is denied: {self._rel(path)}")

    def _ensure_under_root(self, path: Path, root_rel: str, *, kind: str = "path") -> None:
        root = self.workspace.resolve(root_rel)
        if path != root and root not in path.parents:
            raise ValueError(f"{kind} path must stay under {root_rel}")

    def _ensure_under_read_root(self, path: Path) -> None:
        if self.read_root_rel is not None:
            self._ensure_under_root(path, self.read_root_rel, kind="read")

    def _is_other_worker_path(self, path: Path) -> bool:
        if not self.deny_other_unverified:
            return False
        rel = self._rel(path)
        prefix = "unverified_lemmas/"
        if not (rel == "unverified_lemmas" or rel.startswith(prefix)):
            return False
        if self.worker_rel is None:
            return True
        worker_rel = self.worker_rel.strip("/")
        return not (rel == worker_rel or rel.startswith(worker_rel + "/") or rel == "unverified_lemmas")


def build_workspace_tool_registry(
    access: RoleWorkspaceAccess,
    *,
    allow_write: bool = False,
    subagent_service: "SubagentService | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        name="read_file",
        description="Read a .md, .py, or .lean file inside the permitted workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "default": access.max_read_chars},
            },
            "required": ["path"],
        },
        handler=lambda args: ToolResult(
            access.read_text(args["path"], max_chars=int(args.get("max_chars", access.max_read_chars)))
        ),
    )
    if allow_write:
        registry.register(
            name="write_file",
            description="Write a UTF-8 text file in the permitted write area.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=lambda args: ToolResult(json.dumps({"path": access.write_text(args["path"], args["content"])}, ensure_ascii=False)),
        )
        registry.register(
            name="str_replace_file",
            description=(
                "Replace an exact substring in a file. "
                "old_str must match exactly once; use write_file for full rewrites."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
            handler=lambda args: ToolResult(
                json.dumps({"path": access.str_replace(args["path"], args["old_str"], args["new_str"])}, ensure_ascii=False)
            ),
        )

    registry.register(
        name="get_child_item",
        description="List files and directories, similar to PowerShell Get-ChildItem.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "recursive": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "default": 200},
            },
            "required": [],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                access.list_dir(
                    args.get("path", "."),
                    recursive=bool(args.get("recursive", False)),
                    max_results=int(args.get("max_results", 200)),
                ),
                ensure_ascii=False,
            )
        ),
    )
    registry.register(
        name="search_files",
        description="Find permitted .md, .py, or .lean files whose filename contains a text fragment.",
        parameters={
            "type": "object",
            "properties": {
                "fragment": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["fragment"],
        },
        handler=lambda args: ToolResult(
            json.dumps(
                access.search_files(
                    args["fragment"],
                    path=args.get("path", "."),
                    max_results=int(args.get("max_results", 50)),
                ),
                ensure_ascii=False,
            )
        ),
    )
    registry.register(
        name="grep",
        description="Search permitted .md, .py, or .lean files and return matching line numbers.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "regex": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "default": 50},
                "context_lines": {"type": "integer", "default": 0},
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
            name="agent",
            description="Call a configured mathematical subagent by type with a self-contained task.",
            parameters={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": subagent_types},
                    "task": {"type": "string"},
                },
                "required": ["type", "task"],
            },
            handler=lambda args: subagent_service.call_tool(args),
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
        execution_gateway: "ExecutionGateway | None" = None,
        session_prefix: str = "subagent",
        digest_queue: "Any | None" = None,
    ) -> None:
        self.suite = suite
        self.client_factory = client_factory
        self.max_depth = max(0, int(max_depth))
        self.file_access_factory = file_access_factory
        self.execution_gateway = execution_gateway
        self.session_prefix = session_prefix
        self.digest_queue = digest_queue

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
            for name in ("read_file", "write_file", "get_child_item", "search_files", "grep"):
                if name not in enabled_tools:
                    enabled_tools.append(name)
        if depth >= self.max_depth and "agent" in enabled_tools:
            enabled_tools = [name for name in enabled_tools if name != "agent"]
        if enabled_tools != list(config.tools):
            config = GeneralAgentConfig(
                name=config.name,
                system_prompt=config.system_prompt,
                tools=enabled_tools,
                max_turns=config.max_turns,
                model_config=config.model_config,
                skills=config.skills,
                metadata=config.metadata,
            )
        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
        )
        result = agent.run(task)
        out = {
            "type": agent_type,
            "session_id": session_id,
            "final_answer": result.final_answer,
            "turns": result.turns,
            "trace": result.trace,
        }
        # Submit trace to digest queue unless we ARE the digest agent (avoid recursion)
        if self.digest_queue is not None and not self.session_prefix.startswith("knowledge_digest"):
            from .knowledge_digest import DigestTask
            self.digest_queue.submit(DigestTask(
                trace_segment=result.trace,
                source_label=f"{self.session_prefix}/{agent_type}",
            ))
        return out

    def _build_subagent_registry(self, *, depth: int, session_id: str) -> ToolRegistry:
        registry = ToolRegistry()
        python_env: dict[str, Any] = {}
        wolfram_session = {"session": None}

        registry.register(
            name="run_python",
            description="Execute Python code in a persistent in-memory environment without filesystem access.",
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
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
            name="run_wolfram",
            description="Execute Wolfram Language code in a short-lived Wolfram session.",
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
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
            file_registry = build_workspace_tool_registry(access, allow_write=True)
            for tool in file_registry.registered_tools():
                registry.register(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    handler=tool.handler,
                )
        if depth < self.max_depth:
            registry.register(
                name="agent",
                description="Call another configured subagent. Recursive depth is bounded.",
                parameters={
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": self.available_types()},
                        "task": {"type": "string"},
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
