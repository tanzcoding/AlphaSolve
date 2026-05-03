from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.utils.event_logger import compose_event_sinks

from .dashboard import make_curator_event_sink

if TYPE_CHECKING:
    from alphasolve.agents.general import GeneralAgentConfig
    from alphasolve.execution import ExecutionGateway
    from alphasolve.utils.log_session import LogSession
    from alphasolve.utils.rich_renderer import PropositionTeamRenderer
    from .tools import ClientFactory, SubagentService


@dataclass
class CuratorTask:
    trace_segment: list[dict[str, Any]]
    source_label: str  # 调度用来源标签，不应原样写入知识库。
    caller_context: dict[str, Any] | None = None
    task_kind: str = "digest"


CURATOR_HEALTH_CHECK_INTERVAL = 4


class CuratorQueue:
    """Serializes curator agent runs in a background thread."""

    def __init__(
        self,
        *,
        knowledge_dir: Path,
        workspace_dir: Path,
        suite,
        client_factory: "ClientFactory",
        execution_gateway: "ExecutionGateway | None" = None,
        log_session: "LogSession | None" = None,
        stop_event: threading.Event | None = None,
        renderer: "PropositionTeamRenderer | None" = None,
    ) -> None:
        self.knowledge_dir = knowledge_dir
        self.workspace_dir = workspace_dir
        self.suite = suite
        self.client_factory = client_factory
        self.execution_gateway = execution_gateway
        self.log_session = log_session
        self.stop_event = stop_event
        self.renderer = renderer
        self._queue: queue.Queue[CuratorTask | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="curator")
        self._started = False
        self._digest_tasks_since_health_check = 0

    def start(self) -> None:
        if not self._started:
            self._started = True
            if self.renderer is not None:
                self.renderer.update_curator_phase("idle", status="idle")
            self._thread.start()

    def stop(self, timeout: float = 60.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout=timeout)

    def submit(self, task: CuratorTask) -> None:
        if self._started:
            if self.renderer is not None:
                self.renderer.enqueue_curator_task(task.source_label)
            self._queue.put(task)
            if task.task_kind == "digest":
                self._digest_tasks_since_health_check += 1
                if self._digest_tasks_since_health_check >= CURATOR_HEALTH_CHECK_INTERVAL:
                    self._digest_tasks_since_health_check = 0
                    health_task = CuratorTask(
                        trace_segment=[],
                        source_label="knowledge-health-check",
                        task_kind="health_check",
                    )
                    if self.renderer is not None:
                        self.renderer.enqueue_curator_task(health_task.source_label)
                    self._queue.put(health_task)

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                break
            try:
                self._run_curator(task)
            except Exception:
                pass

    def _run_curator(self, task: CuratorTask) -> None:
        config: GeneralAgentConfig | None = self.suite.subagents.get("curator")
        if config is None:
            return

        from alphasolve.agents.general import GeneralPurposeAgent
        from .tools import RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry

        access = RoleWorkspaceAccess(
            workspace=_make_workspace(self.workspace_dir),
            read_root_rel="knowledge",
            write_root_rel="knowledge",
            destructive_protected_file_names=("index.md", "common-errors.md"),
        )
        subagent_svc = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=1,
            execution_gateway=self.execution_gateway,
            session_prefix="curator",
            log_session=self.log_session,
            stop_event=self.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess(
                workspace=_make_workspace(self.workspace_dir),
                read_root_rel="knowledge",
            ),
        )
        registry = build_workspace_tool_registry(
            access,
            allow_write=True,
            allow_manage=True,
            allow_delete=True,
            subagent_service=subagent_svc,
        )

        if task.task_kind == "health_check":
            task_prompt = _health_check_prompt()
        else:
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
                    "In addition to normal knowledge updates, carefully read the verifier's review "
                    "to understand what mistake was made. "
                    "Append up to 3 general error patterns to `knowledge/common-errors.md` when useful. "
                    "Each bullet must describe a reusable pattern of mistakes that the generator tends to make, "
                    "not a specific failed proposition, reviewer, worker, round, attempt, or source label. "
                    "Keep patterns general enough to apply across different problems. "
                    "Do not add bullets for issues already covered. "
                    "`knowledge/common-errors.md` must contain at most 15 error patterns; if it already has 15 patterns "
                    "and a genuinely new one should be added, first merge, compress, or abstract existing related patterns "
                    "so the final file still has no more than 15."
                )
            else:
                extra = "Do not modify `knowledge/common-errors.md`."
            task_prompt = (
                "# Trace Segment for Knowledge Base\n\n"
                f"```json\n{trace_text}\n```\n\n"
                "Update the knowledge base in `knowledge/` based on this trace segment. "
                "Trace metadata is for private triage only; do not copy source labels, worker names, proposition IDs, "
                "generator/verifier/reviser roles, round numbers, attempt numbers, or session IDs into the knowledge base. "
                "If `caller_context` is present, use it to understand the mathematical context, not as provenance text. "
                "Maintain the knowledge base as a problem-specific wiki with detailed derivations, reusable observations, "
                "and carefully organized topic pages. "
                "At the start of the task, read `knowledge/index.md` before browsing or editing other wiki entries. "
                "If an entry is becoming too long for useful LLM reads, split it into a topic folder with focused subtopic pages "
                "and a local `index.md`, rather than scattering fragments in the knowledge root. "
                f"{extra} "
                "Before finishing, make sure `knowledge/index.md` still describes the current entries accurately as a route map."
            )

        curator_sink = self.log_session.create_curator_sink() if self.log_session is not None else None
        curator_success = False
        if self.renderer is not None:
            self.renderer.set_curator_model(_model_name(config, suite=self.suite))
            self.renderer.start_curator_task(task.source_label)
        try:
            agent = GeneralPurposeAgent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=compose_event_sinks(
                    make_curator_event_sink(self.renderer),
                    curator_sink,
                ),
                stop_event=self.stop_event,
            )
            agent.run(task_prompt)
            curator_success = True
        finally:
            if curator_sink is not None:
                curator_sink.close()
            if self.renderer is not None:
                self.renderer.finish_curator_task(success=curator_success)
        _update_entry_metadata(access.touched_paths())


def _make_workspace(workspace_dir: Path):
    from alphasolve.agents.general import Workspace
    return Workspace(workspace_dir)


def _trace_kind(source_label: str) -> str:
    lowered = source_label.lower()
    for kind in ("verifier", "reviser", "generator", "theorem_checker", "orchestrator"):
        if kind in lowered:
            return kind
    return "subagent"


def _model_name(config: "GeneralAgentConfig", *, suite) -> str:
    ref = str(config.model_config or "").strip()
    if not ref:
        return ""
    if ref in suite.models:
        return str(suite.models[ref].get("model", ref))
    preset = ref.upper()
    if not preset.endswith("_CONFIG"):
        preset += "_CONFIG"
    model_config = getattr(AlphaSolveConfig, preset, None)
    if isinstance(model_config, dict):
        return str(model_config.get("model", ref))
    return ref


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


def _health_check_prompt() -> str:
    return (
        "# Knowledge Base Health Check\n\n"
        "Perform a focused maintenance pass on `knowledge/`. This task is not based on a new mathematical trace.\n\n"
        "Read `knowledge/index.md` first, then inspect the directory shape with ListDir/Glob and targeted reads. "
        "Keep the pass practical: make small organization fixes immediately, but only do larger splits or renames "
        "when the current structure is clearly making the wiki hard to navigate.\n\n"
        "Check these items:\n"
        "- `knowledge/index.md` should be a route map with sections like Start Here, Current Bottlenecks, Main Routes, "
        "Failed Routes And Pitfalls, Tools And Lemmas, and All Entries. It should not be a giant flat summary list.\n"
        "- The knowledge root should stay quiet. Broad or oversized topics should live in topic folders with their own "
        "local `index.md`; do not scatter many sibling fragments at the root.\n"
        "- If an entry is too long for useful later reads, split it into a topic folder with focused subtopic files and "
        "preserve cross-links. Files above about 700 lines deserve scrutiny; files above 1000 lines usually need splitting.\n"
        "- Check for stale links, missing entries in the root route map, confusing names, redundant pages, and obvious duplicates.\n"
        "- Keep `knowledge/common-errors.md` concise and capped at 15 error patterns. If it has more than 15 bullets, "
        "or if several bullets describe similar mistakes, consolidate them by abstracting their shared failure mode "
        "into one broader reusable pattern. Look for entries that can be subsumed by a more general error pattern. "
        "Do not add new error patterns in this health check.\n\n"
        "Do not record source labels, worker names, proposition IDs, round numbers, attempts, or session IDs. "
        "Before finishing, make sure `knowledge/index.md` accurately describes the current live structure."
    )


def _update_entry_metadata(touched_paths: tuple[Path, ...]) -> None:
    """统一维护普通词条的 modification_count frontmatter。"""
    for md_file in touched_paths:
        if md_file.name in {"index.md", "common-errors.md"}:
            continue
        if not md_file.exists() or not md_file.is_file() or md_file.suffix.lower() != ".md":
            continue
        _increment_modification_count(md_file)


def _increment_modification_count(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    count = 1
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            body = text[end + 4:]
            for line in frontmatter.splitlines():
                if not line.startswith("modification_count:"):
                    continue
                try:
                    count = int(line.split(":", 1)[1].strip()) + 1
                except ValueError:
                    count = 1
                break
    if not body.startswith("\n"):
        body = "\n" + body
    path.write_text(f"---\nmodification_count: {count}\n---{body}", encoding="utf-8")


def _remove_problem_section_from_index(text: str) -> str:
    problem_marker = "\n## Problem\n"
    entries_marker = "\n## Entries\n"
    start = text.find(problem_marker)
    if start == -1:
        return text
    end = text.find(entries_marker, start)
    if end == -1:
        return text
    prefix = text[:start].rstrip("\n")
    suffix = text[end + 1:].lstrip("\n")
    return prefix + "\n\n" + suffix


def init_knowledge_base(knowledge_dir: Path, problem_text: str) -> None:
    """Initialize the knowledge wiki skeleton."""
    del problem_text
    index = knowledge_dir / "index.md"
    if not index.exists():
        index.write_text(
            "# Knowledge Index\n\n"
            "## Start Here\n\n"
            "_No entries yet._\n\n"
            "## Current Bottlenecks\n\n"
            "_No entries yet._\n\n"
            "## Main Routes\n\n"
            "_No entries yet._\n\n"
            "## Failed Routes And Pitfalls\n\n"
            "_No entries yet._\n\n"
            "## Tools And Lemmas\n\n"
            "_No entries yet._\n\n"
            "## All Entries\n\n"
            "_No entries yet._\n",
            encoding="utf-8",
        )
    else:
        current = index.read_text(encoding="utf-8")
        normalized = _remove_problem_section_from_index(current)
        if normalized != current:
            index.write_text(normalized, encoding="utf-8")
    log = knowledge_dir / "log.md"
    if log.is_file():
        log.unlink()
    errors = knowledge_dir / "common-errors.md"
    if not errors.exists():
        errors.write_text("# Common Proof Errors\n\n", encoding="utf-8")
