from __future__ import annotations

import concurrent.futures
import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.agents.general import AgentRunError, GeneralPurposeAgent, Workspace
from alphasolve.agents.general.tool_registry import ToolRegistry, ToolResult
from alphasolve.utils.event_logger import compose_event_sinks

from .dashboard import make_orchestrator_event_sink
from .worker import Worker, WorkerRunResult
from .project import ProjectLayout
from .solution import write_solution
from .tools import ClientFactory, RoleWorkspaceAccess, SubagentService, build_workspace_tool_registry

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway
    from alphasolve.utils.log_session import LogSession
    from alphasolve.utils.rich_renderer import PropositionTeamRenderer
    from .knowledge_digest import KnowledgeDigestQueue


@dataclass(frozen=True)
class OrchestratorRunResult:
    final_answer: str
    trace: list[dict[str, Any]]
    worker_results: list[WorkerRunResult] = field(default_factory=list)
    solution_path: Path | None = None


class WorkerManager:
    DEFAULT_WAIT_TIMEOUT_SECONDS = 3600.0

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
        digest_queue: KnowledgeDigestQueue | None = None,
        log_session: LogSession | None = None,
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
        self.results: list[WorkerRunResult] = []
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.digest_queue = digest_queue
        self.log_session = log_session
        self.stop_event = threading.Event()
        self.solution_path: Path | None = None
        self.solved_result: WorkerRunResult | None = None

    def spawn(self, hint: str | None = None) -> dict[str, Any]:
        self._collect_done()
        if self.solved_result is not None:
            return {
                "spawned": False,
                "reason": "problem_already_solved",
                "solution_path": str(self.solution_path) if self.solution_path else None,
            }
        if len(self.active) >= self.max_workers:
            return {
                "spawned": False,
                "reason": "parallelism_limit_reached",
                "active_workers": sorted(self.active.values()),
                "max_workers": self.max_workers,
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
            digest_queue=self.digest_queue,
            stop_event=self.stop_event,
            log_session=self.log_session,
        )
        worker_id = worker.worker_id
        if self.renderer is not None:
            self.renderer.register_worker(
                worker_id,
                verified_ctx_size=self._verified_count(),
                remaining_capacity=max(0, self.max_workers - len(self.active) - 1),
            )
        future = self.executor.submit(worker.run)
        self.active[future] = worker_id
        return {
            "spawned": True,
            "worker_id": worker_id,
            "active_count": len(self.active),
            "max_workers": self.max_workers,
        }

    def wait(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        self._collect_done()
        if not self.active:
            return {"completed": [], "active_workers": [], "message": "no active workers"}
        timeout = self.DEFAULT_WAIT_TIMEOUT_SECONDS if timeout_seconds is None else max(300.0, float(timeout_seconds))
        done, _ = concurrent.futures.wait(
            list(self.active.keys()),
            timeout=timeout,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        if not done:
            return {
                "completed": [],
                "active_workers": sorted(self.active.values()),
                "timed_out": True,
                "timeout_seconds": timeout,
                "message": f"no worker finished within {timeout:g} seconds",
            }
        completed = self._consume_done(done)
        payload = {
            "completed": completed,
            "active_workers": sorted(self.active.values()),
        }
        if self.solved_result is not None:
            payload["solved"] = True
            payload["solution_path"] = str(self.solution_path) if self.solution_path else None
            payload["message"] = "problem solved; orchestration should stop"
        return payload

    def close(self, *, timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.active and timeout > 0:
            done, _ = concurrent.futures.wait(
                list(self.active.keys()),
                timeout=timeout,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            self._consume_done(done)
        self._collect_done()

    def _collect_done(self) -> None:
        done = [future for future in self.active if future.done()]
        self._consume_done(done)

    def _consume_done(self, done) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        for future in list(done):
            worker_id = self.active.pop(future, None)
            if worker_id is None:
                continue
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

    def _verified_count(self) -> int:
        return verified_count(self.layout.verified_dir)

    def _append_worker_result_log(self, payload: dict[str, Any]) -> None:
        try:
            self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
            with (self.layout.logs_dir / "worker_results.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass


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
        digest_queue: KnowledgeDigestQueue | None = None,
        log_session: LogSession | None = None,
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
        self.digest_queue = digest_queue
        self.log_session = log_session

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
                digest_queue=self.digest_queue,
                log_session=self.log_session,
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
                file_access_factory=lambda: RoleWorkspaceAccess(
                    workspace=Workspace(self.layout.workspace_dir),
                    deny_read_rel="unverified_propositions",
                ),
            )
            try:
                registry = self._build_registry(manager, subagents=subagents)
                config = self.suite.agents["orchestrator"]
                agent = GeneralPurposeAgent(
                    config=config,
                    client=self.client_factory(config),
                    tool_registry=registry,
                    event_sink=compose_event_sinks(
                        make_orchestrator_event_sink(self.renderer),
                        orchestrator_log_sink,
                    ),
                )
                result = agent.run(self._task())
            except AgentRunError as exc:
                error_final_answer = str(exc)
                error_trace = exc.trace
            finally:
                manager.close()
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

    def _build_registry(self, manager: WorkerManager, *, subagents: SubagentService | None = None) -> ToolRegistry:
        access = RoleWorkspaceAccess(workspace=Workspace(self.layout.workspace_dir))
        registry = build_workspace_tool_registry(access, allow_write=False)
        registry.register(
            name="Agent",
            description="Spawn one worker with an optional orchestrator hint. This call returns immediately. The worker attempts to prove a proposition and verify it.",
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
            description="Wait until any one active worker finishes, returning its lifecycle result. Blocks until a worker completes or timeout is reached.",
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "Maximum seconds to wait before returning active worker status.",
                        "default": WorkerManager.DEFAULT_WAIT_TIMEOUT_SECONDS,
                        "minimum": 300,
                        "maximum": 3600,
                    },
                },
                "required": [],
            },
            handler=lambda args: self._wait_tool(manager, args),
        )
        if subagents is not None:
            registry.register(
                name="Review",
                description="Launch a research_reviewer subagent to survey verified_propositions/ and knowledge/, compare against problem.md, and recommend research directions. Use this when there are too many files to read directly yourself. The reviewer returns a structured report with current state, key files worth reading, gaps, and suggested next directions.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "What to focus on (e.g. 'survey all verified results related to boundedness') or leave general."},
                    },
                    "required": [],
                },
                handler=lambda args: subagents.call_tool({"type": "research_reviewer", "task": args.get("task", "Survey verified_propositions/ and knowledge/, compare against problem.md, and recommend research directions.")}),
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
                "what has been proved and what remains.\n"
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
                "Use `TaskOutput` only to receive worker results — it blocks until one worker's lifecycle ends. "
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
    return sum(1 for path in verified_dir.glob("*.md") if path.is_file())


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


def _solution_final_answer(solution_path: Path | None) -> str:
    if solution_path is None:
        return "Problem solved."
    return f"Problem solved. Solution written to {solution_path}."
