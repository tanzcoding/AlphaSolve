from __future__ import annotations

import concurrent.futures
import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphasolve.agents.general import AgentRunError, GeneralPurposeAgent, Workspace
from alphasolve.agents.general.tool_registry import ToolRegistry, ToolResult

from .dashboard import make_orchestrator_event_sink
from .lemma_worker import LemmaWorker, LemmaWorkerRunResult
from .project import ProjectLayout
from .solution import write_solution
from .tools import ClientFactory, RoleWorkspaceAccess, build_workspace_tool_registry

if TYPE_CHECKING:
    from alphasolve.execution import ExecutionGateway
    from alphasolve.utils.rich_renderer import LemmaTeamRenderer
    from .knowledge_digest import KnowledgeDigestQueue


@dataclass(frozen=True)
class OrchestratorRunResult:
    final_answer: str
    trace: list[dict[str, Any]]
    worker_results: list[LemmaWorkerRunResult] = field(default_factory=list)
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
        renderer: LemmaTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        digest_queue: KnowledgeDigestQueue | None = None,
    ) -> None:
        self.layout = layout
        self.suite = suite
        self.client_factory = client_factory
        self.max_workers = max(1, int(max_workers))
        self.max_verify_rounds = max_verify_rounds
        self.verifier_scaling_factor = max(1, int(verifier_scaling_factor))
        self.subagent_max_depth = subagent_max_depth
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self.active: dict[concurrent.futures.Future, int] = {}
        self.results: list[LemmaWorkerRunResult] = []
        self.next_worker_id = 0
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.digest_queue = digest_queue
        self.stop_event = threading.Event()
        self.solution_path: Path | None = None
        self.solved_result: LemmaWorkerRunResult | None = None

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
        worker_id = self.next_worker_id
        self.next_worker_id += 1
        if self.renderer is not None:
            self.renderer.register_worker(
                worker_id,
                verified_ctx_size=self._verified_count(),
                remaining_capacity=max(0, self.max_workers - len(self.active) - 1),
            )
        worker = LemmaWorker(
            layout=self.layout,
            suite=self.suite,
            client_factory=self.client_factory,
            worker_id=worker_id,
            worker_hint=hint,
            max_verify_rounds=self.max_verify_rounds,
            verifier_scaling_factor=self.verifier_scaling_factor,
            subagent_max_depth=self.subagent_max_depth,
            renderer=self.renderer,
            execution_gateway=self.execution_gateway,
            digest_queue=self.digest_queue,
            stop_event=self.stop_event,
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
            return {"completed": [], "active_workers": [], "message": "no active lemmaworkers"}
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
                "message": f"no lemmaworker finished within {timeout:g} seconds",
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
        renderer: LemmaTeamRenderer | None = None,
        execution_gateway: ExecutionGateway | None = None,
        digest_queue: KnowledgeDigestQueue | None = None,
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

    def run(self) -> OrchestratorRunResult:
        if self.renderer is not None:
            self.renderer.update_pool(verified_count=self._verified_count())
            self.renderer.update_orchestrator_phase("starting", status="running")
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
        )
        result = None
        error_final_answer = ""
        error_trace: list[dict[str, Any]] = []
        try:
            registry = self._build_registry(manager)
            config = self.suite.agents["orchestrator"]
            agent = GeneralPurposeAgent(
                config=config,
                client=self.client_factory(config),
                tool_registry=registry,
                event_sink=make_orchestrator_event_sink(self.renderer),
            )
            result = agent.run(self._task())
        except AgentRunError as exc:
            error_final_answer = str(exc)
            error_trace = exc.trace
        finally:
            manager.close()

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

    def _build_registry(self, manager: WorkerManager) -> ToolRegistry:
        access = RoleWorkspaceAccess(workspace=Workspace(self.layout.workspace_dir))
        registry = build_workspace_tool_registry(access, allow_write=False)
        registry.register(
            name="spawn_worker",
            description="Spawn one lemmaworker with an optional orchestrator hint. This call returns immediately.",
            parameters={
                "type": "object",
                "properties": {
                    "hint": {"type": "string"},
                },
                "required": [],
            },
            handler=lambda args: self._spawn_tool(manager, args),
        )
        registry.register(
            name="wait",
            description="Wait until any one active lemmaworker finishes, returning its lifecycle result.",
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
                "You are the orchestrator. Your role is that of a research director: read existing lemmas "
                "and the knowledge log to understand what has already been established, identify gaps or "
                "promising directions, then spawn lemmaworkers with targeted hints that push the overall "
                "proof forward. Do not solve or verify lemmas yourself.\n\n"
                "Your session succeeds if and only if a lemma that solves the original problem appears in "
                "the `verified_lemmas/` directory. There is no other way to succeed. Do not stop until "
                "that happens — if workers finish without solving the problem, keep spawning new ones with "
                "better hints.\n\n"
                "Workflow:\n"
                "1. Read `knowledge/log.md` and any relevant lemma files to survey current progress.\n"
                "2. Identify what is missing or what would most advance the solution.\n"
                "3. Spawn workers with specific, well-motivated hints based on your analysis.\n"
                "4. Call `wait` to block until a worker finishes, then read its output and repeat.\n\n"
                "Use `wait` only to receive worker results — it blocks until one worker's lifecycle ends. "
                "It may return `timed_out: true` if no worker finishes within the requested timeout; if repeated "
                "timeouts show no progress, report the stall so the outer Ralph loop can restart orchestration.\n\n"
                "You may issue multiple tool calls in a single turn: for example, read several lemma files "
                "in parallel, or spawn multiple workers at once with distinct hints targeting different "
                "sub-problems. Avoid spawning workers with redundant or near-identical hints."
            ),
            f"Maximum concurrent lemmaworkers: {self.max_workers}",
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


def _worker_result_payload(result: LemmaWorkerRunResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_id": result.worker_id,
        "status": result.status,
        "summary": result.summary,
        "worker_dir": str(result.worker_dir),
        "solved_problem": result.solved_problem,
    }
    if result.lemma_file:
        payload["lemma_file"] = str(result.lemma_file)
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
