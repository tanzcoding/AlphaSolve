from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.solver.logging.event_log import compose_event_sinks

from alphasolve.solver.ui.dashboard import make_curator_event_sink

if TYPE_CHECKING:
    from alphasolve.agent import AgentConfig
    from alphasolve.solver.execution import ExecutionGateway
    from alphasolve.solver.logging.log_session import LogSession
    from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer
    from .client_factory import ClientFactory
    from .subagent_service import SubagentService


@dataclass
class CuratorTask:
    trace_segment: list[dict[str, Any]]
    source_label: str  # 调度用来源标签，不应原样写入知识库。
    caller_context: dict[str, Any] | None = None
    task_kind: str = "digest"
    audit_path: Path | None = None
    artifact_path: Path | None = None


CURATOR_HEALTH_CHECK_INTERVAL = 4
CURATOR_OVERSIZED_ENTRY_LINE_LIMIT = 250


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
        self._touched_paths: set[Path] = set()
        self._touched_paths_lock = threading.Lock()
        self._active_tasks = 0
        self._active_tasks_lock = threading.Lock()

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

    def touched_paths(self) -> tuple[Path, ...]:
        with self._touched_paths_lock:
            return tuple(sorted(self._touched_paths, key=lambda item: item.as_posix()))

    def _record_touched_paths(self, paths: tuple[Path, ...]) -> None:
        if not paths:
            return
        with self._touched_paths_lock:
            self._touched_paths.update(path.resolve() for path in paths)

    def has_active_task(self) -> bool:
        with self._active_tasks_lock:
            return self._active_tasks > 0

    def _set_task_active(self, active: bool) -> None:
        with self._active_tasks_lock:
            if active:
                self._active_tasks += 1
            else:
                self._active_tasks = max(0, self._active_tasks - 1)

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                break
            self._set_task_active(True)
            try:
                self._run_curator(task)
            except Exception:
                pass
            finally:
                self._set_task_active(False)

    def _run_curator(self, task: CuratorTask) -> None:
        config: AgentConfig | None = self.suite.subagents.get("curator")
        if config is None:
            return

        from alphasolve.agent import Agent
        from .blocker_registry import PersistentBlockerRegistry, register_curated_blocker_registry_tool
        from .subagent_service import SubagentService
        from .tool_runtime import build_solver_tool_registry
        from .workspace_access import RoleWorkspaceAccess

        access = RoleWorkspaceAccess.curator(_make_workspace(self.workspace_dir))
        subagent_svc = SubagentService(
            suite=self.suite,
            client_factory=self.client_factory,
            max_depth=1,
            execution_gateway=self.execution_gateway,
            session_prefix="curator",
            log_session=self.log_session,
            stop_event=self.stop_event,
            file_access_factory=lambda: RoleWorkspaceAccess.curator_subagent(
                _make_workspace(self.workspace_dir)
            ),
        )
        extra_registrars = ()
        if task.task_kind == "portfolio_checkpoint" and task.artifact_path is not None:
            checkpoint_id = task.artifact_path.parent.name
            blocker_registry = PersistentBlockerRegistry(self.workspace_dir)
            extra_registrars = (
                lambda tool_registry: register_curated_blocker_registry_tool(
                    tool_registry,
                    blocker_registry=blocker_registry,
                    checkpoint_id=checkpoint_id,
                    replace=True,
                ),
            )
        registry = build_solver_tool_registry(
            access,
            agent_config=config,
            dispatcher=subagent_svc,
            extra_registrars=extra_registrars,
        )

        if task.task_kind == "health_check":
            task_prompt = _health_check_prompt(self.knowledge_dir)
        elif task.task_kind == "portfolio_checkpoint":
            task_prompt = _portfolio_checkpoint_prompt(task.artifact_path)
        elif task.task_kind == "strategy_transition":
            task_prompt = _strategy_transition_prompt(task.artifact_path)
        elif task.task_kind == "progress_audit":
            task_prompt = _progress_audit_prompt(task.audit_path)
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
            agent = Agent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=compose_event_sinks(
                    make_curator_event_sink(self.renderer),
                    curator_sink,
                    self.log_session.token_usage_sink("curator")
                    if self.log_session is not None else None,
                    self.log_session.run_log_sink("curator")
                    if self.log_session is not None else None,
                ),
                stop_event=self.stop_event,
            )
            agent.run(task_prompt)
            if task.task_kind == "portfolio_checkpoint" and task.artifact_path is not None:
                checkpoint_id = task.artifact_path.parent.name
                if not PersistentBlockerRegistry(self.workspace_dir).is_checkpoint_curated(checkpoint_id):
                    raise RuntimeError(
                        f"curator did not persist blocker curation for checkpoint {checkpoint_id}"
                    )
            curator_success = True
        finally:
            if curator_sink is not None:
                curator_sink.close()
            if self.renderer is not None:
                self.renderer.finish_curator_task(success=curator_success)
        touched_paths = access.touched_paths()
        _update_entry_metadata(touched_paths)
        self._record_touched_paths(touched_paths)


def _make_workspace(workspace_dir: Path):
    from alphasolve.agent import Workspace
    return Workspace(workspace_dir)


def _trace_kind(source_label: str) -> str:
    lowered = source_label.lower()
    for kind in ("verifier", "reviser", "generator", "theorem_checker", "orchestrator"):
        if kind in lowered:
            return kind
    return "subagent"


def _model_name(config: "AgentConfig", *, suite) -> str:
    del suite  # unused; preserved for signature compatibility
    return config.effective_tier()


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


def _strategy_transition_prompt(artifact_path: Path | None) -> str:
    path_text = ""
    if artifact_path is not None:
        try:
            path_text = artifact_path.resolve().relative_to(artifact_path.parents[2]).as_posix()
        except (ValueError, IndexError):
            path_text = str(artifact_path)
    return (
        "# Orchestrator Strategy Transition Curation\n\n"
        "Read the transition fact at `" + (path_text or "(missing transition fact)") + "` and its cited process audit. "
        "Record this as a process observation in `knowledge/portfolio/strategy-transitions.md` and update "
        "`knowledge/portfolio/process-audit-history.md` if appropriate.\n\n"
        "State only what the evidence supports: the prior process verdict, the terminal gap, why the old context was reset, "
        "and what the next context is required to reconsider. Do not claim that a new direction was successful before a later "
        "outcome supports it. When subsequent checkpoint briefs arrive, compare their actual targets, rubrics, and outcomes "
        "against this transition. Do not copy internal worker/session identifiers or timestamps into knowledge files."
    )


def _portfolio_checkpoint_prompt(artifact_path: Path | None) -> str:
    path_text = ""
    if artifact_path is not None:
        try:
            path_text = artifact_path.resolve().relative_to(artifact_path.parents[2]).as_posix()
        except (ValueError, IndexError):
            path_text = str(artifact_path)
    return (
        "# Portfolio Checkpoint Curation\n\n"
        "Read the checkpoint brief first: `" + (path_text or "(missing checkpoint brief)") + "`. "
        "It combines the current process audit, settled worker outcomes, rubric/impact evidence, prior checkpoint comparison, "
        "and orchestrator session/context-reset events. You may read cited files under `progress_audits/` and `curation_records/` "
        "to verify the comparison, but never edit anything outside `knowledge/`.\n\n"
        "Before writing knowledge, read `curation_records/blocker_registry.json` when it exists. Then call "
        "`CuratePersistentBlockers` exactly once before finishing. You—not the process auditor—own the semantic decision "
        "whether outcome difficulties are the same mathematical blocker across different routes.\n\n"
        "Mandatory identity reconciliation:\n"
        "1. List every material present difficulty in `current_difficulties`, including the audit candidate if it has one.\n"
        "2. Compare EACH current difficulty against EACH active historical blocker in `blocker_relations`; no pair may be omitted.\n"
        "3. Use `same` only when the mathematical obligation is identical despite different statements, directions, methods, or local lemmas. "
        "It MUST reuse an old `blocker_id`; if several historical IDs are same, select one canonical old ID and declare all others "
        "same with that `canonical_blocker_id`, so runtime merges them.\n"
        "4. Use `distinct` for genuinely independent obligations, `unresolved` only when evidence cannot decide equivalence, and "
        "`superseded` only when a new named blocker replaces the old obligation/gate. Never create a fresh ID merely because wording or route changed.\n"
        "5. Group every supporting outcome sequence by actual direction/gap/method and select one concrete gate direction/gap. For outcomes with a "
        "Structured Difficulty Handoff, compare the exact blocking obligation, last verified step, and failed inference; wording similarity alone is not evidence. "
        "The tool derives counts from sequences and persists the classification across restarts. If no new repeated blocker exists, still submit continuing "
        "current difficulties and their relations to every active blocker; resolve an existing blocker only when evidence discharges it.\n\n"
        "Maintain these durable knowledge files rather than creating an isolated narrative only:\n"
        "- `knowledge/portfolio/current-strategy.md`: current terminal gap, verified facts that bear on it, and what a useful next proposition must accomplish.\n"
        "- `knowledge/portfolio/attempt-patterns.md`: reusable attempt patterns. For each pattern state conditions, contrast between attempts, outcome, reusable rule, confidence, and exceptions.\n"
        "- `knowledge/portfolio/strategy-transitions.md`: compare strategy/context generations only when the evidence shows a genuine change; record what was abandoned, what changed, and whether the new attempt improved.\n"
        "- `knowledge/portfolio/process-audit-history.md`: compact evolution of verdicts, terminal gaps, repeated avoided obligations, and whether subsequent outcomes validated the audit.\n"
        "- `knowledge/portfolio/index.md`, and link this folder from `knowledge/index.md`.\n\n"
        "Evidence rules:\n"
        "- Separate **verified mathematical facts** from **process observations** and **strategy recommendations**.\n"
        "- A correct local proposition is not progress unless the brief shows it connected to the terminal gap and its rubric/impact evidence supports that claim.\n"
        "- Compare attempts by target/gap, method family, rubric result, summary, avoided obligation, verification status, and later audit verdict; do not merely list them chronologically.\n"
        "- Preserve useful contrasts: explain why one line advanced while a superficially similar line was incidental, rejected, or repeatedly avoided the same obligation.\n"
        "- Do not copy worker IDs, session IDs, timestamps, raw prompts, or source labels into knowledge files. Refer to mathematical targets and strategy generations descriptively instead.\n"
        "- Do not overwrite a prior lesson merely because a new audit disagrees; record the condition or evidence that explains the difference.\n"
        "- Do not modify the checkpoint brief, raw audit, evidence, or curation records.\n"
    )


def _progress_audit_prompt(audit_path: Path | None) -> str:
    audit_text = ""
    checkpoint_id = "unknown-checkpoint"
    if audit_path is not None:
        checkpoint_id = audit_path.parent.name or checkpoint_id
        try:
            audit_text = audit_path.read_text(encoding="utf-8")[:24000]
        except (OSError, UnicodeDecodeError):
            audit_text = ""
    return (
        "# Strategic Progress Audit Curation\n\n"
        "A runtime-generated strategic audit is provided below. It is evidence about whether completed workers are advancing "
        "the original problem, not a mathematical proof by itself. Read it carefully, then update the knowledge base.\n\n"
        "Required actions:\n"
        f"- Create or update `knowledge/progress-audits/{checkpoint_id}-summary.md`.\n"
        "- State the audit verdict, the current terminal gap, recurring avoided obligations, and the one recommended next target.\n"
        "- Extract reusable failure patterns or blocker descriptions, but do not treat rejected work or knowledge notes as proved facts.\n"
        "- Update `knowledge/progress-audits/index.md` and ensure `knowledge/index.md` routes to this directory.\n"
        "- Never copy worker IDs, session IDs, source labels, timestamps, or raw runtime metadata into knowledge files.\n"
        "- Do not alter the original audit; this task writes only derived knowledge summaries.\n\n"
        "## Raw Audit Report\n\n"
        + (audit_text or "Audit report was unavailable; record that the checkpoint needs re-audit.")
    )


def _health_check_prompt(knowledge_dir: Path | None = None) -> str:
    scan_text = _knowledge_health_scan(knowledge_dir) if knowledge_dir is not None else ""
    scan_section = ""
    if scan_text:
        scan_section = (
            "\n\nProgram scan before curator:\n"
            f"{scan_text}\n"
            "Use this scan as a triage list, then inspect the files before renaming, moving, editing wiki notes, or splitting references with SplitReference."
        )
    return (
        "# Knowledge Base Health Check\n\n"
        "Perform a focused maintenance pass on `knowledge/`. This task is not based on a new mathematical trace.\n\n"
        "Read `knowledge/index.md` first, then inspect the directory shape with ListDir/Glob and targeted reads. "
        "Keep the pass practical: make small organization fixes immediately, but only do larger splits, moves, or renames "
        "when the current structure is clearly making the wiki hard to navigate.\n\n"
        "Check these items:\n"
        "- `knowledge/index.md` should be a route map of immediate children, not a giant flat summary list.\n"
        "- Each topic directory should have its own `index.md`; every index should track only immediate child files and folders.\n"
        "- Keep broad or oversized topics in topic folders. Files over 250 lines should usually be split, "
        "except `knowledge/common-errors.md` which stays as one compressed file.\n"
        "- Keep user-provided papers, OCR markdown, and personal notes under `knowledge/references/`; rename and move reference files for organization, but do not Write/Edit reference text.\n"
        "- Check for stale links, confusing names, redundant pages, obvious duplicates, and program-reported untracked files.\n"
        "- Keep `knowledge/common-errors.md` concise and capped at 15 error patterns. If it has more than 15 bullets, "
        "or if several bullets describe similar mistakes, consolidate them by abstracting their shared failure mode "
        "into one broader reusable pattern. Look for entries that can be subsumed by a more general error pattern. "
        "Do not add new error patterns in this health check.\n\n"
        "Do not record source labels, worker names, proposition IDs, round numbers, attempts, or session IDs. "
        "Before finishing, make sure `knowledge/index.md` accurately describes the current live structure."
        f"{scan_section}"
    )


def _knowledge_health_scan(knowledge_dir: Path) -> str:
    if not knowledge_dir.exists():
        return "- `knowledge/` does not exist yet."

    missing = _find_untracked_markdown(knowledge_dir)
    oversized = _find_oversized_markdown(knowledge_dir)
    common_errors_lines = _common_errors_line_count(knowledge_dir)
    lines: list[str] = []
    if missing:
        lines.append("Untracked markdown:")
        lines.extend(f"- {item}" for item in missing)
    if oversized:
        if lines:
            lines.append("")
        lines.append(f"Markdown over {CURATOR_OVERSIZED_ENTRY_LINE_LIMIT} lines:")
        lines.extend(f"- {path} ({line_count} lines)" for path, line_count in oversized)
    if common_errors_lines is not None and common_errors_lines > CURATOR_OVERSIZED_ENTRY_LINE_LIMIT:
        if lines:
            lines.append("")
        lines.append("Common errors maintenance:")
        lines.append(
            f"- `knowledge/common-errors.md` is {common_errors_lines} lines. Keep it within "
            f"{CURATOR_OVERSIZED_ENTRY_LINE_LIMIT} lines and within 15 common error patterns; "
            "if it has too many patterns, distill and abstract shared failure modes until it has at most 15."
        )
    if not lines:
        return "- No untracked markdown or oversized markdown files detected."
    return "\n".join(lines)


def _find_untracked_markdown(knowledge_dir: Path) -> list[str]:
    entries: list[str] = []
    for md_file in sorted(knowledge_dir.rglob("*.md")):
        if md_file.name == "index.md" and md_file.parent == knowledge_dir:
            continue
        if md_file.name == "index.md":
            tracked_child = md_file.parent
            parent_dir = md_file.parent.parent
            rel = _knowledge_rel(tracked_child, knowledge_dir) + "/"
            expected = _knowledge_rel(parent_dir / tracked_child.name / "index.md", knowledge_dir)
        else:
            tracked_child = md_file
            parent_dir = md_file.parent
            rel = _knowledge_rel(md_file, knowledge_dir)
            expected = rel
        parent_index = parent_dir / "index.md"
        parent_rel = _knowledge_rel(parent_index, knowledge_dir)
        if not parent_index.is_file():
            entries.append(f"`knowledge/{rel}` has no parent index `knowledge/{parent_rel}`")
            continue
        try:
            index_text = parent_index.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            index_text = parent_index.read_text(encoding="utf-8", errors="replace")
        if not _index_tracks_child(index_text, tracked_child, parent_dir, knowledge_dir):
            entries.append(f"`knowledge/{expected}` is not tracked by `knowledge/{parent_rel}`")
    return entries


def _find_oversized_markdown(knowledge_dir: Path) -> list[tuple[str, int]]:
    oversized: list[tuple[str, int]] = []
    for md_file in sorted(knowledge_dir.rglob("*.md")):
        if md_file.name == "common-errors.md":
            continue
        line_count = _markdown_line_count(md_file)
        if line_count > CURATOR_OVERSIZED_ENTRY_LINE_LIMIT:
            oversized.append((f"knowledge/{_knowledge_rel(md_file, knowledge_dir)}", line_count))
    return oversized


def _common_errors_line_count(knowledge_dir: Path) -> int | None:
    common_errors = knowledge_dir / "common-errors.md"
    if not common_errors.is_file():
        return None
    return _markdown_line_count(common_errors)


def _markdown_line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except UnicodeDecodeError:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)


def _index_tracks_child(index_text: str, child: Path, parent_dir: Path, knowledge_dir: Path) -> bool:
    text = index_text.lower()
    candidates: set[str] = set()
    if child.is_dir():
        local_dir = child.name
        rel_dir = _knowledge_rel(child, knowledge_dir)
        candidates.update({local_dir, f"{local_dir}/index", rel_dir, f"{rel_dir}/index"})
    else:
        local = child.relative_to(parent_dir).as_posix()
        rel = _knowledge_rel(child, knowledge_dir)
        local_stem = local[:-3] if local.lower().endswith(".md") else local
        rel_stem = rel[:-3] if rel.lower().endswith(".md") else rel
        candidates.update({local, local_stem, rel, rel_stem, child.name, child.stem})
    for candidate in candidates:
        lowered = candidate.lower()
        if f"[[{lowered}]]" in text or f"({lowered})" in text or lowered in text:
            return True
    return False


def _knowledge_rel(path: Path, knowledge_dir: Path) -> str:
    return path.relative_to(knowledge_dir).as_posix()


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
            "## References\n\n"
            "- [[references/index]] - user-provided papers, OCR markdown, and personal notes.\n\n"
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
    references = knowledge_dir / "references"
    references.mkdir(exist_ok=True)
    references_index = references / "index.md"
    if not references_index.exists():
        references_index.write_text(
            "# References\n\n"
            "_No user-provided papers or notes yet._\n",
            encoding="utf-8",
        )
