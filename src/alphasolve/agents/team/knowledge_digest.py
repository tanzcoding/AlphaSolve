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
    from alphasolve.utils.log_session import LogSession
    from .tools import ClientFactory, SubagentService


@dataclass
class DigestTask:
    trace_segment: list[dict[str, Any]]
    source_label: str  # 调度用来源标签，不应原样写入知识库。
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
        log_session: "LogSession | None" = None,
    ) -> None:
        self.knowledge_dir = knowledge_dir
        self.workspace_dir = workspace_dir
        self.suite = suite
        self.client_factory = client_factory
        self.execution_gateway = execution_gateway
        self.log_session = log_session
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
            read_root_rel="knowledge",
            write_root_rel="knowledge",
        )
        subagent_svc = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=1,
            execution_gateway=self.execution_gateway,
            session_prefix="knowledge_digest",
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=_make_workspace(self.workspace_dir),
                read_root_rel="knowledge",
            ),
        )
        registry = build_workspace_tool_registry(access, allow_write=True, subagent_service=subagent_svc)

        trace_kind = _trace_kind(task.source_label)
        is_verifier_final = trace_kind == "verifier" and _is_final_verifier_trace(task.trace_segment)
        payload: Any = {
            "trace_kind": trace_kind,
            "trace": task.trace_segment,
        }
        if task.caller_context:
            payload = {
                "trace_kind": trace_kind,
                "caller_context": task.caller_context,
                "subagent_trace": task.trace_segment,
            }
        trace_text = json.dumps(payload, ensure_ascii=False, indent=2)
        if is_verifier_final:
            extra = (
                "This trace contains a verifier's final review of a generator's proposition. "
                "In addition to normal knowledge updates, carefully read the verifier's review, "
                "then read the generator's `proposition.md` to understand what mistake was made. "
                "Append up to 3 general error patterns to `knowledge/common-errors.md` when useful. "
                "Each bullet must describe a reusable pattern of mistakes that the generator tends to make, "
                "not a specific failed proposition, reviewer, worker, round, attempt, or source label. "
                "Keep patterns general enough to apply across different problems. "
                "Do not add bullets for issues already covered."
            )
        else:
            extra = "Do not modify `knowledge/common-errors.md`."
        task_prompt = (
            "# Trace Segment for Knowledge Digest\n\n"
            f"```json\n{trace_text}\n```\n\n"
            "Update the knowledge base in `knowledge/` based on this trace segment. "
            "Trace metadata is for private triage only; do not copy source labels, worker names, proposition IDs, "
            "generator/verifier/reviser roles, round numbers, attempt numbers, or session IDs into the knowledge base. "
            "If `caller_context` is present, use it to understand the mathematical context, not as provenance text. "
            "Follow the required directory-listing and candidate-search workflow from the system prompt. "
            "Create or update topic-based wiki entries. "
            f"{extra} "
            "Append a one-line topic-based entry to `knowledge/log.md` when done."
        )

        digest_sink = self.log_session.create_digest_sink() if self.log_session is not None else None
        try:
            agent = GeneralPurposeAgent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=digest_sink,
            )
            agent.run(task_prompt)
        finally:
            if digest_sink is not None:
                digest_sink.close()
        _update_entry_metadata(self.knowledge_dir)


def _make_workspace(workspace_dir: Path):
    from alphasolve.agents.general import Workspace
    return Workspace(workspace_dir)


def _trace_kind(source_label: str) -> str:
    lowered = source_label.lower()
    for kind in ("verifier", "reviser", "generator", "theorem_checker", "orchestrator"):
        if kind in lowered:
            return kind
    return "subagent"


def _is_final_verifier_trace(trace_segment: list[dict[str, Any]]) -> bool:
    """Return True if the trace segment contains a verifier's final response.

    Verifier-attempt traces submitted from worker.py carry a single-element
    segment with ``{"role": "verifier_attempt", "content": ...}``.
    Intermediate subagent traces do not have this marker.
    """
    return any(
        isinstance(item, dict) and item.get("role") == "verifier_attempt"
        for item in trace_segment
    )


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
    errors = knowledge_dir / "common-errors.md"
    if not errors.exists():
        errors.write_text("# Common Proof Errors\n\n", encoding="utf-8")
