from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from alphasolve.agents.lemma_pool import LemmaPool
from alphasolve.agents.lemma_worker import LemmaWorker
from alphasolve.agents.lemmaworker import LemmaWorkerContext
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.llms.utils import LLMClient
from alphasolve.utils.log_session import LogSession
from alphasolve.utils.logger import Logger
from alphasolve.utils.rich_renderer import LemmaTeamRenderer


@dataclass
class PoolRunResult:
    solved: bool
    summary: Optional[str]


class LemmaPoolOrchestrator:
    def __init__(
        self,
        *,
        pool: LemmaPool,
        logger: Logger,
        log_session: LogSession,
        problem: str,
        hint: Optional[str],
        execution_gateway,
        parallelism_limit: int,
        print_to_console: Optional[bool] = None,
    ):
        self.pool = pool
        self.logger = logger
        self.log_session = log_session
        self.problem = problem
        self.hint = hint
        self.execution_gateway = execution_gateway
        self.parallelism_limit = max(1, int(parallelism_limit))
        self.print_to_console = logger.print_to_console_default if print_to_console is None else bool(print_to_console)
        self._next_worker_id = 0
        self._renderer: Optional[LemmaTeamRenderer] = LemmaTeamRenderer() if self.print_to_console else None
        if self._renderer is not None:
            self._renderer.update_pool(
                capacity_verified=self.pool.capacity_verified,
                verified_count=len(self.pool.snapshot_verified()),
                solved=self.pool.is_solved(),
            )
        self.llm = LLMClient(
            module="orchestrator",
            config=AlphaSolveConfig.ORCHESTRATOR_CONFIG,
            logger=logger,
            execution_gateway=execution_gateway,
        )

    def run(self) -> PoolRunResult:
        return asyncio.run(self.run_async())

    async def run_async(self) -> PoolRunResult:
        old_renderer = getattr(self.logger, "console_renderer", None)
        old_worker_id = getattr(self.logger, "console_worker_id", None)
        if self._renderer is not None:
            self.logger.console_renderer = self._renderer
            self.logger.console_worker_id = None
            self._renderer.start()
            self._renderer.update_orchestrator_phase("starting")
        self.logger.log_print(
            f"event=pool_start parallelism_limit={self.parallelism_limit} capacity_verified={self.pool.capacity_verified}",
            module="pool_orchestrator",
        )

        try:
            active: dict[asyncio.Task, int] = {}
            while True:
                while (
                    not self.pool.is_solved()
                    and not self.pool.is_full()
                    and len(active) < self.parallelism_limit
                ):
                    worker_id = self._next_worker_id
                    self._next_worker_id += 1

                    print_to_console = self.print_to_console

                    task = asyncio.create_task(self._run_one_worker(worker_id, print_to_console))
                    active[task] = worker_id
                    self.logger.log_print(
                        f"event=worker_spawn worker_id={worker_id} print_to_console={print_to_console}",
                        module="pool_orchestrator",
                    )

                if not active:
                    break

                if self._renderer is not None:
                    if self.pool.is_solved():
                        self._renderer.update_orchestrator_phase("draining", status="solved")
                    elif self.pool.is_full():
                        self._renderer.update_orchestrator_phase("draining", status="full")
                    else:
                        self._renderer.update_orchestrator_phase("waiting")

                done, _ = await asyncio.wait(set(active), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    worker_id = active.pop(task)

                    try:
                        result = task.result()
                    except Exception as exc:
                        if self._renderer is not None:
                            self._renderer.finish_worker(worker_id, status="failed", summary=str(exc))
                            self._renderer.remove_worker(worker_id)
                        self.logger.log_print(
                            f"event=worker_failed worker_id={worker_id} error={exc}",
                            module="pool_orchestrator",
                            level="ERROR",
                        )
                        continue

                    if result.status == "verified":
                        if self._renderer is not None:
                            self._renderer.update_phase(worker_id, "theorem-check")
                            self._renderer.update_orchestrator_phase("theorem-check")
                        is_theorem = await self._check_is_theorem(
                            statement=result.lemma.get("statement", "")
                        )
                        result.is_theorem = is_theorem
                        result.lemma["is_theorem"] = is_theorem
                        self.logger.log_print(
                            f"event=orchestrator_check_is_theorem worker_id={worker_id} is_theorem={is_theorem}",
                            module="pool_orchestrator",
                        )

                    if self._renderer is not None:
                        self._renderer.update_orchestrator_phase("committing")
                    decision = self.pool.commit(result)
                    self.log_session.update_version()
                    if self._renderer is not None:
                        self._renderer.record_commit(
                            accepted=decision.accepted,
                            status=decision.status,
                            solved=decision.solved,
                            duplicate_of=decision.duplicate_of,
                            verified_count=len(self.pool.snapshot_verified()),
                        )
                        self._renderer.finish_worker(
                            worker_id,
                            status=decision.status,
                            solved=decision.solved,
                            summary=self._format_worker_summary(result, decision),
                        )
                        self._renderer.remove_worker(worker_id)
                    self.logger.log_print(
                        f"event=worker_finished worker_id={worker_id} status={decision.status} solved={decision.solved}",
                        module="pool_orchestrator",
                    )

                if self.pool.is_solved() or self.pool.is_full():
                    continue

            if self._renderer is not None:
                self._renderer.update_orchestrator_phase(
                    "complete",
                    status="solved" if self.pool.is_solved() else "full" if self.pool.is_full() else "idle",
                )
            return PoolRunResult(solved=self.pool.is_solved(), summary=None)
        finally:
            if self._renderer is not None:
                self._renderer.update_pool(
                    verified_count=len(self.pool.snapshot_verified()),
                    solved=self.pool.is_solved(),
                )
                self._renderer.stop()
            self.logger.console_renderer = old_renderer
            self.logger.console_worker_id = old_worker_id

    async def _run_one_worker(self, worker_id: int, print_to_console: bool):
        worker_logger = self.log_session.worker_logger(
            worker_id,
            print_to_console=print_to_console,
            console_renderer=self._renderer if print_to_console else None,
        )
        worker = LemmaWorker(
            logger=worker_logger,
            execution_gateway=self.execution_gateway,
            print_to_console=print_to_console,
        )

        verified_snapshot = self.pool.snapshot_verified()
        remaining_capacity = self.pool.remaining_verified_capacity()
        if self._renderer is not None and print_to_console:
            self._renderer.register_worker(
                worker_id,
                verified_ctx_size=len(verified_snapshot),
                remaining_capacity=remaining_capacity,
            )

        ctx = LemmaWorkerContext(
            problem=self.problem,
            hint=self.hint,
            verified_snapshot=verified_snapshot,
            remaining_capacity=remaining_capacity,
            run_id=self.log_session.run_id,
            worker_id=worker_id,
        )
        return await worker.run_async(ctx)

    async def _check_is_theorem(self, *, statement: str) -> bool:
        return await asyncio.to_thread(self._check_is_theorem_sync, statement=statement)

    def _check_is_theorem_sync(self, *, statement: str) -> bool:
        verified_lemmas = self.pool.snapshot_verified()
        shared = {
            "problem": self.problem,
            "hint": self.hint,
            "lemmas": verified_lemmas,
            "current_lemma_id": None,
            "result_summary": None,
        }

        for _ in range(AlphaSolveConfig.CHECK_IS_THEOREM_TIMES):
            check_message = (
                "Check if the following statement fully addresses the problem "
                "(do NOT check if the statement is mathematically correct - only check if it fully resolves the problem). "
                "Output ONLY 'Yes' or 'No' without any explanation.\n\n"
                f"Problem: {self.problem}\n\nStatement: {statement}"
            )
            response, _, _ = self.llm.get_result(
                messages=[{"role": "user", "content": check_message}],
                tools=[],
                shared=shared,
            )
            if response.strip().lower() != "yes":
                return False
        return True

    def _format_worker_summary(self, result, decision) -> str:
        if decision.solved:
            prefix = "solved"
        elif decision.accepted:
            prefix = "accepted"
        elif decision.duplicate_of is not None:
            prefix = f"duplicate:{decision.duplicate_of}"
        else:
            prefix = decision.status

        statement = " ".join(str(result.lemma.get("statement", "")).split())
        if not statement:
            return prefix
        if len(statement) > 140:
            statement = statement[:137].rstrip() + "..."
        return f"{prefix} | {statement}"
