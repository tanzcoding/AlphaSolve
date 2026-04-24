from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alphasolve.agents.general import GeneralAgentConfig
    from alphasolve.execution import ExecutionGateway
    from .tools import ClientFactory, SubagentService


@dataclass
class DigestTask:
    trace_segment: list[dict[str, Any]]
    source_label: str  # e.g. "worker-0001/generator/compute_subagent"
    caller_context: dict[str, Any] | None = None


class KnowledgeDigestQueue:
    """Serializes digest agent runs in a background thread."""

    def __init__(
        self,
        *,
        knowledge_dir: Path,
        workspace_dir: Path,
        suite,
        client_factory: "ClientFactory",
        execution_gateway: "ExecutionGateway | None" = None,
    ) -> None:
        self.knowledge_dir = knowledge_dir
        self.workspace_dir = workspace_dir
        self.suite = suite
        self.client_factory = client_factory
        self.execution_gateway = execution_gateway
        self._queue: queue.Queue[DigestTask | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="knowledge-digest")
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def stop(self, timeout: float = 60.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout=timeout)

    def submit(self, task: DigestTask) -> None:
        if self._started:
            self._queue.put(task)

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                break
            try:
                self._run_digest(task)
            except Exception:
                pass

    def _run_digest(self, task: DigestTask) -> None:
        config: GeneralAgentConfig | None = self.suite.subagents.get("knowledge_digest")
        if config is None:
            return

        from alphasolve.agents.general import GeneralPurposeAgent
        from .tools import RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry

        access = RoleWorkspaceAccess(
            workspace=_make_workspace(self.workspace_dir),
            write_root_rel="knowledge",
        )
        subagent_svc = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=1,
            execution_gateway=self.execution_gateway,
            session_prefix="knowledge_digest",
        )
        registry = build_workspace_tool_registry(access, allow_write=True, subagent_service=subagent_svc)

        payload: Any = task.trace_segment
        if task.caller_context:
            payload = {
                "source_label": task.source_label,
                "caller_context": task.caller_context,
                "subagent_trace": task.trace_segment,
            }
        trace_text = json.dumps(payload, ensure_ascii=False, indent=2)
        task_prompt = (
            f"# New trace segment from: {task.source_label}\n\n"
            f"```json\n{trace_text}\n```\n\n"
            "Update the knowledge base in `knowledge/` based on this trace segment. "
            "If `caller_context` is present, it contains the caller's new reasoning since the previous subagent "
            "call plus metadata about the current subagent call; use it together with `subagent_trace`. "
            "Read `knowledge/index.md` first to understand existing entries. "
            "Create or update wiki-style entries. "
            "Append a one-line entry to `knowledge/log.md` when done."
        )

        agent = GeneralPurposeAgent(
            config=config,
            client=self.client_factory(config),
            tool_registry=registry,
        )
        agent.run(task_prompt)
        _update_entry_metadata(self.knowledge_dir)


def _make_workspace(workspace_dir: Path):
    from alphasolve.agents.general import Workspace
    return Workspace(workspace_dir)


def _update_entry_metadata(knowledge_dir: Path) -> None:
    """Increment modification_count in frontmatter of recently modified .md files."""
    now = time.time()
    for md_file in knowledge_dir.glob("*.md"):
        if md_file.name in {"index.md", "log.md"}:
            continue
        if now - md_file.stat().st_mtime > 10:
            continue
        _increment_modification_count(md_file)


def _increment_modification_count(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    end = text.find("\n---", 3)
    if end == -1:
        return
    frontmatter = text[3:end]
    rest = text[end + 4:]
    lines = frontmatter.splitlines()
    new_lines = []
    found = False
    for line in lines:
        if line.startswith("modification_count:"):
            try:
                count = int(line.split(":", 1)[1].strip()) + 1
            except ValueError:
                count = 1
            new_lines.append(f"modification_count: {count}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append("modification_count: 1")
    path.write_text("---" + "\n".join(new_lines) + "\n---" + rest, encoding="utf-8")


def init_knowledge_base(knowledge_dir: Path, problem_text: str) -> None:
    """Create index.md and log.md if they don't exist."""
    index = knowledge_dir / "index.md"
    if not index.exists():
        index.write_text(
            "# Knowledge Index\n\n"
            "## Problem\n\n"
            f"{problem_text.strip()}\n\n"
            "## Entries\n\n"
            "_No entries yet._\n",
            encoding="utf-8",
        )
    log = knowledge_dir / "log.md"
    if not log.exists():
        log.write_text("# Knowledge Log\n\n", encoding="utf-8")
