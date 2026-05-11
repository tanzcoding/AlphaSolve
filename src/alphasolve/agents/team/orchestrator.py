from __future__ import annotations

import concurrent.futures
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.agents.general import AgentRunError, GeneralPurposeAgent, Workspace
from alphasolve.agents.general.tool_registry import ToolRegistry, ToolResult
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.utils.event_logger import compose_event_sinks

from .dashboard import make_orchestrator_event_sink
from .worker import Worker, WorkerRunResult
from .project import ProjectLayout
from .solution import write_solution
from .tools import ClientFactory, RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry, register_agent_tool

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway
    from alphasolve.utils.log_session import LogSession
    from alphasolve.utils.rich_renderer import PropositionTeamRenderer
    from .curator import CuratorQueue


@dataclass(frozen=True)
class OrchestratorRunResult:
    final_answer: str
    trace: list[dict[str, Any]]
    worker_results: list[WorkerRunResult] = field(default_factory=list)
    solution_path: Path | None = None


class RuntimeInjectionMonitor:
    def __init__(self, *, layout: ProjectLayout, curator_queue: CuratorQueue | None = None) -> None:
        self.layout = layout
        self.curator_queue = curator_queue
        self._known_reference_files = self._scan_reference_files()

    def check(self) -> dict[str, Any] | None:
        hint_updated = self._sync_changed_hint()
        new_reference_files = self._new_reference_files()
        if not hint_updated and not new_reference_files:
            return None

        messages: list[str] = []
        if hint_updated:
            messages.append(
                "hint.md has changed and was copied from the project root into workspace/hint.md; "
                "this may be a human expert injection. Read hint.md before deciding the next action."
            )
        if new_reference_files:
            refs = ", ".join(new_reference_files)
            messages.append(
                "New files appeared under workspace/knowledge/references during this run; "
                f"this may be a human expert injection. Read these files: {refs}."
            )
        return {
            "hint_md_updated": hint_updated,
            "new_reference_files": new_reference_files,
            "message": " ".join(messages),
        }

    def _sync_changed_hint(self) -> bool:
        source = self.layout.hint_path or (self.layout.project_root / "hint.md")
        target = self.layout.workspace_dir / "hint.md"
        if not source.is_file():
            return False
        source_bytes = source.read_bytes()
        target_bytes = target.read_bytes() if target.is_file() else None
        if target_bytes == source_bytes:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True

    def _new_reference_files(self) -> list[str]:
        if (
            self.curator_queue is not None
            and hasattr(self.curator_queue, "has_active_task")
            and self.curator_queue.has_active_task()
        ):
            return []
        current = self._scan_reference_files()
        added = current - self._known_reference_files
        if not added:
            return []

        curator_touched = self._curator_touched_reference_files()
        human_added = sorted(
            rel
            for rel in added
            if rel not in curator_touched
            and rel != "knowledge/references/index.md"
        )
        self._known_reference_files.update(added)
        return human_added

    def _scan_reference_files(self) -> set[str]:
        references_dir = self.layout.knowledge_dir / "references"
        if not references_dir.is_dir():
            return set()
        out: set[str] = set()
        for path in references_dir.rglob("*"):
            if path.is_file():
                out.add(path.resolve().relative_to(self.layout.workspace_dir).as_posix())
        return out

    def _curator_touched_reference_files(self) -> set[str]:
        if self.curator_queue is None or not hasattr(self.curator_queue, "touched_paths"):
            return set()
        references_dir = self.layout.knowledge_dir / "references"
        out: set[str] = set()
        for path in self.curator_queue.touched_paths():
            resolved = Path(path).resolve()
            if resolved.is_file() and (resolved == references_dir or references_dir in resolved.parents):
                out.add(resolved.relative_to(self.layout.workspace_dir).as_posix())
        return out


class WorkerManager:
    DEFAULT_WAIT_TIMEOUT_SECONDS = 3600.0
    VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD = 40

    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int,
        max_verify_rounds: int,
        verifier_scaling_factor: int,
        subagent_max_depth: int,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.max_workers = max(1, int(max_workers))
        self.max_verify_rounds = max_verify_rounds
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = subagent_max_depth
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self.active: dict[concurrent.futures.Future, str] = {}
        self.active_info: dict[str, dict[str, Any]] = {}
        self.active_info_lock = threading.Lock()
        self.results: list[WorkerRunResult] = []
        self.completed_backlog: list[dict[str, Any]] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.log_session = log_session
        self.stop_event = stop_event or threading.Event()
        self.solution_path: Path | None = None
        self.solved_result: WorkerRunResult | None = None
        self.injection_monitor = RuntimeInjectionMonitor(layout=self.layout, curator_queue=self.curator_queue)

    def spawn(self, hint: str | None = None) -> dict[str, Any]:
        self._collect_done()
        if self.solved_result is not None:
            return {
                "spawned": False,
                "reason": "problem_already_solved",
                "solution_path": str(self.solution_path) if self.solution_path else None,
                **self._pool_status(),
            }
        if len(self.active) >= self.max_workers:
            return {
                "spawned": False,
                "reason": "parallelism_limit_reached",
                **self._pool_status(),
            }
        worker = Worker(
            layout=self.layout,
            suite=self.suite,
            client_factory=self.client_factory,
            worker_hint=hint,
            max_verify_rounds=self.max_verify_rounds,
            verifier_scaling_factor=self.verifier_scaling_factor,
            subagent_max_depth=self.subagent_max_depth,
            renderer=self.renderer,
            execution_gateway=self.execution_gateway,
            curator_queue=self.curator_queue,
            stop_event=self.stop_event,
            log_session=self.log_session,
        )
        worker_id = worker.worker_id
        worker.progress_callback = lambda phase, status, worker_id=worker_id: self._update_worker_progress(
            worker_id,
            phase,
            status,
        )
        if self.renderer is not None:
            self.renderer.register_worker(
                worker_id,
                verified_ctx_size=self._verified_count(),
                remaining_capacity=max(0, self.max_workers - len(self.active) - 1),
            )
        with self.active_info_lock:
            self.active_info[worker_id] = {
                "worker_id": worker_id,
                "worker_dir": str(worker.worker_dir),
                "started_at": time.time(),
                "phase": "spawned",
                "phase_status": "running",
                "phase_updated_at": time.time(),
            }
        future = self.executor.submit(worker.run)
        self.active[future] = worker_id
        return {
            "spawned": True,
            "worker_id": worker_id,
            **self._pool_status(),
        }

    def wait(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        self._collect_done()
        if self.completed_backlog:
            completed = list(self.completed_backlog)
            self.completed_backlog.clear()
            return self._with_runtime_updates(self._wait_payload(completed))
        if not self.active:
            return self._with_runtime_updates({"completed": [], **self._pool_status(), "message": "no active workers"})
        timeout = self.DEFAULT_WAIT_TIMEOUT_SECONDS if timeout_seconds is None else max(1200.0, float(timeout_seconds))
        done, _ = concurrent.futures.wait(
            list(self.active.keys()),
            timeout=timeout,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        if not done:
            return self._with_runtime_updates({
                "completed": [],
                "timed_out": True,
                "timeout_seconds": timeout,
                "message": f"no worker finished within {timeout:g} seconds",
                **self._pool_status(),
            })
        return self._with_runtime_updates(self._wait_payload(self._consume_done(done)))

    def close(self, *, timeout: float = 5.0, graceful: bool = False) -> None:
        wait_timeout = max(0.0, float(timeout))
        if graceful:
            self.executor.shutdown(wait=False, cancel_futures=False)
            done, not_done = self._wait_for_active(timeout=wait_timeout)
            self._consume_done(done)
            if not_done:
                if self.renderer is not None:
                    remaining = ", ".join(self.active.get(future, "unknown") for future in sorted(not_done, key=id))
                    self.renderer.log(
                        None,
                        "graceful worker shutdown timed out; leaving unfinished workers to unwind in background: "
                        + remaining,
                        module="shutdown",
                        level="WARNING",
                    )
                # 优雅关闭只等有限时间，超时后转入强制收尾，避免某个挂起的调用把整个退出流程永久卡死。
                self.stop_event.set()
                self.executor.shutdown(wait=False, cancel_futures=True)
                self._collect_done()
                return
            self._collect_done()
            self.executor.shutdown(wait=True, cancel_futures=False)
            return

        self.stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        done, _ = self._wait_for_active(timeout=wait_timeout)
        self._consume_done(done)
        self._collect_done()

    def _wait_for_active(
        self,
        *,
        timeout: float,
    ) -> tuple[set[concurrent.futures.Future], set[concurrent.futures.Future]]:
        if not self.active:
            return set(), set()
        return concurrent.futures.wait(
            list(self.active.keys()),
            timeout=timeout,
            return_when=concurrent.futures.ALL_COMPLETED,
        )

    def _collect_done(self) -> None:
        done = [future for future in self.active if future.done()]
        self.completed_backlog.extend(self._consume_done(done))

    def _consume_done(self, done) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        for future in list(done):
            worker_id = self.active.pop(future, None)
            if worker_id is None:
                continue
            with self.active_info_lock:
                self.active_info.pop(worker_id, None)
            try:
                result = future.result()
            except Exception as exc:
                if self.renderer is not None:
                    self.renderer.finish_worker(worker_id, status="failed", summary=str(exc))
                payload = {"worker_id": worker_id, "status": "failed", "summary": str(exc)}
                self._append_worker_result_log(payload)
                completed.append(payload)
                continue
            self.results.append(replace(result, trace=[]))
            if result.solved_problem and result.verified_file is not None and self.solved_result is None:
                self.solved_result = result
                self.stop_event.set()
                self.solution_path = write_solution(self.layout, result.verified_file)
            if self.renderer is not None:
                self.renderer.finish_worker(
                    worker_id,
                    status=result.status,
                    summary=_short_summary(result.summary),
                )
                self.renderer.record_commit(
                    accepted=result.status == "verified",
                    status=result.status,
                    solved=result.solved_problem,
                    verified_count=self._verified_count(),
                )
            payload = _worker_result_payload(result)
            self._append_worker_result_log(payload)
            completed.append(payload)
        return completed

    def _wait_payload(self, completed: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "completed": completed,
            **self._pool_status(),
        }
        if self.solved_result is not None:
            payload["solved"] = True
            payload["solution_path"] = str(self.solution_path) if self.solution_path else None
            payload["message"] = "problem solved; orchestration should stop"
        return payload

    def _with_runtime_updates(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates = self.injection_monitor.check()
        if updates is not None:
            payload["human_expert_updates"] = updates
        organization_prompt = self._verified_propositions_organization_prompt()
        if organization_prompt is not None:
            payload["verified_propositions_organization"] = organization_prompt
        return payload

    def _verified_propositions_organization_prompt(self) -> dict[str, Any] | None:
        overloaded_dirs = self._overloaded_verified_proposition_dirs()
        threshold = self.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD
        if not overloaded_dirs:
            return None
        return {
            "threshold": threshold,
            "directories": overloaded_dirs,
            "message": "Organize these verified_propositions directories before spawning more work: "
            + ", ".join(item["path"] for item in overloaded_dirs),
        }

    def _overloaded_verified_proposition_dirs(self) -> list[dict[str, Any]]:
        if not self.layout.verified_dir.is_dir():
            return []
        threshold = self.VERIFIED_PROPOSITIONS_ORGANIZATION_THRESHOLD
        overloaded: list[dict[str, Any]] = []
        for directory in sorted(
            (path for path in self.layout.verified_dir.rglob("*") if path.is_dir()),
            key=lambda item: item.relative_to(self.layout.verified_dir).as_posix(),
        ):
            direct_markdown_count = sum(1 for path in directory.glob("*.md") if path.is_file())
            if direct_markdown_count > threshold:
                overloaded.append({
                    "path": directory.relative_to(self.layout.workspace_dir).as_posix(),
                    "markdown_file_count": direct_markdown_count,
                })
        root_markdown_count = sum(1 for path in self.layout.verified_dir.glob("*.md") if path.is_file())
        if root_markdown_count > threshold:
            overloaded.insert(0, {
                "path": "verified_propositions",
                "markdown_file_count": root_markdown_count,
            })
        return overloaded

    def _pool_status(self) -> dict[str, Any]:
        active_workers = [
            self._active_worker_payload(worker_id)
            for worker_id in sorted(self.active.values())
        ]
        return {
            "active_count": len(active_workers),
            "active_worker_ids": [item["worker_id"] for item in active_workers],
            "active_workers": active_workers,
            "max_workers": self.max_workers,
            "available_worker_slots": max(0, self.max_workers - len(active_workers)),
        }

    def _active_worker_payload(self, worker_id: str) -> dict[str, Any]:
        with self.active_info_lock:
            info = dict(self.active_info.get(worker_id, {}))
        elapsed = max(0.0, time.time() - float(info.get("started_at") or time.time()))
        progress = f"running for {elapsed:.0f}s; {_format_worker_phase(str(info.get('phase') or 'starting'))}"
        return {
            "worker_id": worker_id,
            "worker_dir": info.get("worker_dir"),
            "progress": progress,
        }

    def _update_worker_progress(self, worker_id: str, phase: str, status: str) -> None:
        with self.active_info_lock:
            info = self.active_info.get(worker_id)
            if info is None:
                return
            info["phase"] = phase
            info["phase_status"] = status
            info["phase_updated_at"] = time.time()

    def _verified_count(self) -> int:
        return verified_count(self.layout.verified_dir)

    def _append_worker_result_log(self, payload: dict[str, Any]) -> None:
        try:
            self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
            with (self.layout.logs_dir / "worker_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            import sys
            print(
                f"[worker_results] failed to write result for {payload.get('worker_id', '?')}: "
                f"{payload.get('status', '?')}",
                file=sys.stderr,
            )


class Orchestrator:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int,
        max_verify_rounds: int,
        verifier_scaling_factor: int,
        subagent_max_depth: int,
        renderer: PropositionTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        curator_queue: CuratorQueue | None = None,
        log_session: LogSession | None = None,
        stop_event: threading.Event | None = None,
        worker_stop_event: threading.Event | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.max_workers = max_workers
        self.max_verify_rounds = max_verify_rounds
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = subagent_max_depth
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.curator_queue = curator_queue
        self.log_session = log_session
        self.stop_event = stop_event
        self.worker_stop_event = worker_stop_event or threading.Event()

    def run(self) -> OrchestratorRunResult:
        if self.renderer is not None:
            self.renderer.update_pool(verified_count=self._verified_count())
            self.renderer.update_orchestrator_phase("starting", status="running")
        orchestrator_log_sink = None
        if self.log_session is not None:
            orchestrator_log_sink = self.log_session.create_orchestrator_sink()
        try:
            manager = WorkerManager(
                layout=self.layout,
                suite=self.suite,
                client_factory=self.client_factory,
                max_workers=self.max_workers,
                max_verify_rounds=self.max_verify_rounds,
                verifier_scaling_factor=self.verifier_scaling_factor,
                subagent_max_depth=self.subagent_max_depth,
                renderer=self.renderer,
                execution_gateway=self.execution_gateway,
                curator_queue=self.curator_queue,
                log_session=self.log_session,
                stop_event=self.worker_stop_event,
            )
            result = None
            error_final_answer = ""
            error_trace: list[dict[str, Any]] = []
            subagents = SubagentService(
                suite=self.suite,
                client_factory=self.client_factory,
                max_depth=0,  # research_reviewer must not delegate further
                execution_gateway=self.execution_gateway,
                session_prefix="orchestrator",
                log_session=self.log_session,
                file_access_factory=lambda: RoleWorkspaceAccess(
                    workspace=Workspace(self.layout.workspace_dir),
                    deny_read_rel="unverified_propositions",
                ),
                stop_event=self.stop_event,
            )
            try:
                registry = self._build_registry(manager, subagents=subagents)
                config = self.suite.agents["orchestrator"]
                if self.renderer is not None:
                    model_name = self._model_name(config)
                    self.renderer.set_orchestrator_model(model_name)
                agent = GeneralPurposeAgent(
                    config=config,
                    client=self.client_factory(config),
                    tool_registry=registry,
                    event_sink=compose_event_sinks(
                        make_orchestrator_event_sink(self.renderer),
                        orchestrator_log_sink,
                    ),
                    stop_event=self.stop_event,
                )
                result = agent.run(self._task())
            except AgentRunError as exc:
                error_final_answer = str(exc)
                error_trace = exc.trace
            finally:
                user_requested_stop = (
                    self.stop_event is not None
                    and self.stop_event.is_set()
                    and manager.solved_result is None
                )
                manager.close(graceful=user_requested_stop)
        finally:
            if orchestrator_log_sink is not None:
                orchestrator_log_sink.close()

        if result is None:
            return OrchestratorRunResult(
                final_answer=error_final_answer,
                trace=error_trace,
                worker_results=list(manager.results),
                solution_path=manager.solution_path,
            )
        return OrchestratorRunResult(
            final_answer=result.final_answer,
            trace=result.trace,
            worker_results=list(manager.results),
            solution_path=manager.solution_path,
        )

    def _model_name(self, config) -> str:
        ref = str(config.model_config or "").strip()
        if not ref:
            return ""
        if ref in self.suite.models:
            return str(self.suite.models[ref].get("model", ref))
        preset = ref.upper()
        if not preset.endswith("_CONFIG"):
            preset += "_CONFIG"
        cfg = getattr(AlphaSolveConfig, preset, None)
        if isinstance(cfg, dict):
            return str(cfg.get("model", ref))
        return ref

    def _build_registry(self, manager: WorkerManager, *, subagents: SubagentService | None = None) -> ToolRegistry:
        access = RoleWorkspaceAccess(
            workspace=Workspace(self.layout.workspace_dir),
            write_root_rel="verified_propositions",
            destructive_protected_file_names=("index.md",),
            preserve_markdown_file_names_on_rename=True,
        )
        registry = build_workspace_tool_registry(access, allow_write=True, allow_manage=True)
        registry.register(
            name="SpawnWorker",
            description=(
                "Spawn one worker with an optional orchestrator hint. This call returns immediately. "
                "The worker attempts to prove a proposition and verify it. The result includes the current "
                "active worker count, active worker IDs, and a short progress snapshot for each active worker."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hint": {"type": "string", "description": "A targeted hint suggesting a direction, method, branch, or local target for this worker."},
                },
                "required": [],
            },
            handler=lambda args: self._spawn_tool(manager, args),
        )
        registry.register(
            name="TaskOutput",
            description=(
                "Wait until any one active worker finishes, returning its lifecycle result plus the current "
                "active worker count, active worker IDs, and short progress snapshots for still-active workers. "
                "Blocks until a worker completes or timeout is reached."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "Maximum seconds to wait before returning active worker status.",
                        "default": WorkerManager.DEFAULT_WAIT_TIMEOUT_SECONDS,
                        "minimum": 1200,
                        "maximum": 3600,
                    },
                },
                "required": [],
            },
            handler=lambda args: self._wait_tool(manager, args),
        )
        if subagents is not None:
            from .tools import GeneralAgentConfig
            register_agent_tool(
                registry,
                agent_config=GeneralAgentConfig(
                    name="_orchestrator_subagent",
                    system_prompt="",
                    tools=["Agent"],
                    tool_parameters={"Agent": {"type": {"enum": ["research_reviewer"]}}},
                ),
                subagent_service=subagents,
            )
        return registry

    def _spawn_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        payload = manager.spawn(args.get("hint"))
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            stop_agent=manager.solved_result is not None,
            stop_answer=_solution_final_answer(manager.solution_path),
        )

    def _wait_tool(self, manager: WorkerManager, args: dict[str, Any]) -> ToolResult:
        timeout_seconds = args.get("seconds")
        payload = manager.wait(timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None)
        if manager.solved_result is not None:
            manager.close()
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            stop_agent=manager.solved_result is not None,
            stop_answer=_solution_final_answer(manager.solution_path),
        )

    def _task(self) -> str:
        hint = self.layout.read_hint()
        parts = [
            "# Problem",
            self.layout.read_problem(),
            "# Project Workspace",
            (
                "You are the orchestrator. Your role is that of a research director: survey what has already "
                "been rigorously established, identify the most promising gaps, and spawn workers with "
                "targeted hints that push the overall proof forward. Do not solve or verify propositions yourself.\n\n"
                "Your session succeeds if and only if a proposition that solves the original problem appears in "
                "the `verified_propositions/` directory. There is no other way to succeed. Do not stop until "
                "that happens — if workers finish without solving the problem, keep spawning new ones with "
                "better hints.\n\n"
                "Workspace layout:\n"
                "- `verified_propositions/` — the ground truth of current progress. Every `.md` file here is a "
                "rigorously verified mathematical result you can build on. Read these to understand exactly "
                "what has been proved and what remains. You are responsible for keeping this directory tidy and easy "
                "to navigate as the research grows: when root-level verified files accumulate or several files clearly "
                "belong to the same route, assumption, obstruction, or technique, organize them into topic folders by "
                "creating folders, renaming folders with Rename, and moving verified files into folders with Move. Never rename a `.md` file: keep the "
                "same filename when moving it. Maintain `verified_propositions/index.md`; create it if missing. "
                "Keep it concise with exactly two main sections: `## Directory`, listing every verified proposition "
                "and roughly what it proves, and `## Current Progress And Insights`, summarizing what remains and "
                "which directions look promising. Keep the second section under 50 lines whenever possible.\n"
                "- `knowledge/` — exploratory notes distilled from past worker runs. These capture ideas, "
                "partial arguments, and observations that workers have encountered but not yet turned into "
                "verified propositions. Treat this as a research notebook: useful for inspiration and for crafting "
                "hints, but nothing here counts as established until it appears in `verified_propositions/`.\n\n"
                "Workflow:\n"
                "1. If `verified_propositions/` or `knowledge/` contain more than a handful of files, use `Review` "
                "to get a structured survey and discover which specific files are worth reading. Do not read "
                "dozens of files yourself — delegate to the reviewer.\n"
                "2. Read the key files the reviewer flagged, then identify the most valuable next proposition.\n"
                "3. Spawn workers via `Agent` with specific, well-motivated hints based on your analysis.\n"
                "4. Call `TaskOutput` to block until a worker finishes, then read its output and repeat.\n\n"
                "Worker lifecycle: each worker first runs `generator` to draft one candidate proposition, then runs "
                "`verifier`; if verification fails and rounds remain, it runs `reviser` and repeats the "
                "`verifier` -> `reviser` loop until a proposition is verified or the worker exhausts its rounds. "
                "After verification, theorem-checking decides whether the new verified proposition solves the "
                "original problem.\n\n"
                "Use `TaskOutput` only to receive worker results — it blocks until one worker's lifecycle ends. "
                "Both `Agent` and `TaskOutput` return `active_count`, `active_worker_ids`, and `active_workers`; "
                "use those fields to keep the number of active workers at or below the maximum. "
                "It may return `timed_out: true` if no worker finishes within the requested timeout; if repeated "
                "timeouts show no progress, report the stall so the outer Ralph loop can restart orchestration.\n\n"
                "You may issue multiple tool calls in a single turn: for example, read several proposition files "
                "in parallel, or call `Agent` multiple times at once with distinct hints targeting different "
                "sub-problems. Avoid spawning workers with redundant or near-identical hints."
            ),
            f"Maximum concurrent workers: {self.max_workers}",
        ]
        if hint:
            parts.extend(["# User Hint", hint])
        return "\n\n".join(parts)

    def _verified_count(self) -> int:
        return verified_count(self.layout.verified_dir)


def verified_count(verified_dir: Path) -> int:
    if not verified_dir.exists():
        return 0
    return sum(1 for path in verified_dir.glob("*.md") if path.is_file() and path.name != "index.md")


def _worker_result_payload(result: WorkerRunResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_id": result.worker_id,
        "status": result.status,
        "summary": result.summary,
        "worker_dir": str(result.worker_dir),
        "solved_problem": result.solved_problem,
    }
    if result.proposition_file:
        payload["proposition_file"] = str(result.proposition_file)
    if result.verified_file:
        payload["verified_file"] = str(result.verified_file)
    if result.review_file:
        payload["review_file"] = str(result.review_file)
    if result.theorem_check_file:
        payload["theorem_check_file"] = str(result.theorem_check_file)
    return payload


def _short_summary(summary: str, *, limit: int = 180) -> str:
    clean = " ".join((summary or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _format_worker_phase(phase: str) -> str:
    clean = " ".join((phase or "starting").split())
    if clean == "generator":
        return "current agent: generator"
    if clean.startswith("verifier_attempt"):
        match = re.search(r"w(\d+)\.(\d+)", clean)
        if match:
            return f"current agent: verifier, round {match.group(1)}, attempt {match.group(2)}"
        return "current agent: verifier"
    if clean.startswith("review_verdict_judge"):
        match = re.search(r"w(\d+)\.(\d+)", clean)
        if match:
            return f"current agent: verifier verdict judge, round {match.group(1)}, attempt {match.group(2)}"
        return "current agent: verifier verdict judge"
    if clean.startswith("reviser"):
        match = re.search(r"w(\d+)", clean)
        if match:
            return f"current agent: reviser, round {match.group(1)}"
        return "current agent: reviser"
    if clean == "theorem_checker":
        return "current agent: theorem_checker"
    if clean == "done":
        return "current agent: done"
    return f"current agent: {clean}"


def _solution_final_answer(solution_path: Path | None) -> str:
    if solution_path is None:
        return "Problem solved."
    return f"Problem solved. Solution written to {solution_path}."
