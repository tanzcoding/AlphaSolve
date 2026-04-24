from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from alphasolve.agents.general import GeneralPurposeAgent, Workspace
from alphasolve.agents.general.tool_registry import ToolRegistry, ToolResult

from .dashboard import make_orchestrator_event_sink
from .lemma_worker import LemmaWorker, LemmaWorkerRunResult
from .project import ProjectLayout
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


class WorkerManager:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int,
        max_verify_rounds: int,
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
        self.subagent_max_depth = subagent_max_depth
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self.active: dict[concurrent.futures.Future, int] = {}
        self.results: list[LemmaWorkerRunResult] = []
        self.next_worker_id = 0
        self.renderer = renderer
        self.execution_gateway = execution_gateway
        self.digest_queue = digest_queue

    def spawn(self, hint: str | None = None) -> dict[str, Any]:
        self._collect_done()
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
            subagent_max_depth=self.subagent_max_depth,
            renderer=self.renderer,
            execution_gateway=self.execution_gateway,
            digest_queue=self.digest_queue,
        )
        future = self.executor.submit(worker.run)
        self.active[future] = worker_id
        return {
            "spawned": True,
            "worker_id": worker_id,
            "active_count": len(self.active),
            "max_workers": self.max_workers,
        }

    def wait(self, seconds: float) -> dict[str, Any]:
        self._collect_done()
        if not self.active:
            return {"completed": [], "active_workers": [], "message": "no active lemmaworkers"}
        timeout = max(0.0, float(seconds))
        done, _ = concurrent.futures.wait(
            list(self.active.keys()),
            timeout=timeout,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        completed = self._consume_done(done)
        return {
            "completed": completed,
            "active_workers": sorted(self.active.values()),
            "wait_seconds": timeout,
        }

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
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
                completed.append({"worker_id": worker_id, "status": "failed", "summary": str(exc)})
                continue
            self.results.append(result)
            if self.renderer is not None:
                self.renderer.finish_worker(
                    worker_id,
                    status=result.status,
                    summary=_short_summary(result.summary),
                )
                self.renderer.record_commit(
                    accepted=result.status == "verified",
                    status=result.status,
                    solved=False,
                    verified_count=self._verified_count(),
                )
            completed.append(_worker_result_payload(result))
        return completed

    def _verified_count(self) -> int:
        return verified_count(self.layout.verified_dir)


class Orchestrator:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        suite,
        client_factory: ClientFactory,
        max_workers: int,
        max_verify_rounds: int,
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
            subagent_max_depth=self.subagent_max_depth,
            renderer=self.renderer,
            execution_gateway=self.execution_gateway,
            digest_queue=self.digest_queue,
        )
        result = None
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
        finally:
            manager.close()

        if result is None:
            return OrchestratorRunResult(final_answer="", trace=[], worker_results=list(manager.results))
        return OrchestratorRunResult(
            final_answer=result.final_answer,
            trace=result.trace,
            worker_results=list(manager.results),
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
            handler=lambda args: ToolResult(json.dumps(manager.spawn(args.get("hint")), ensure_ascii=False)),
        )
        registry.register(
            name="wait",
            description="Wait for active lemmaworkers to finish, returning any lifecycle results produced during the wait.",
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "default": 600},
                },
                "required": [],
            },
            handler=lambda args: ToolResult(json.dumps(manager.wait(float(args.get("seconds", 600))), ensure_ascii=False)),
        )
        return registry

    def _task(self) -> str:
        hint = self.layout.read_hint()
        parts = [
            "# Problem",
            self.layout.read_problem(),
            "# Project Workspace",
            (
                "You are the orchestrator. You may inspect workspace files and spawn lemmaworkers, "
                "but you must not solve or verify lemmas yourself. Use `spawn_worker` to create workers. "
                "Use `wait` when the worker limit is reached or when you need worker results."
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
    }
    if result.lemma_file:
        payload["lemma_file"] = str(result.lemma_file)
    if result.verified_file:
        payload["verified_file"] = str(result.verified_file)
    if result.review_file:
        payload["review_file"] = str(result.review_file)
    return payload


def _short_summary(summary: str, *, limit: int = 180) -> str:
    clean = " ".join((summary or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."
