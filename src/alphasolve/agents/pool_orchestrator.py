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
    ):
        self.pool = pool
        self.logger = logger
        self.log_session = log_session
        self.problem = problem
        self.hint = hint
        self.execution_gateway = execution_gateway
        self.parallelism_limit = max(1, int(parallelism_limit))
        self._next_worker_id = 0
        self._console_owner: Optional[int] = None
        self.llm = LLMClient(
            module="orchestrator",
            config=AlphaSolveConfig.ORCHESTRATOR_CONFIG,
            logger=logger,
            execution_gateway=execution_gateway,
        )

    def run(self) -> PoolRunResult:
        return asyncio.run(self.run_async())

    async def run_async(self) -> PoolRunResult:
        self.logger.log_print(
            f"event=pool_start parallelism_limit={self.parallelism_limit} capacity_verified={self.pool.capacity_verified}",
            module="pool_orchestrator",
        )

        active: dict[asyncio.Task, int] = {}
        while True:
            while (
                not self.pool.is_solved()
                and not self.pool.is_full()
                and len(active) < self.parallelism_limit
            ):
                worker_id = self._next_worker_id
                self._next_worker_id += 1

                if self._console_owner is None:
                    self._console_owner = worker_id
                print_to_console = self._console_owner == worker_id

                task = asyncio.create_task(self._run_one_worker(worker_id, print_to_console))
                active[task] = worker_id
                self.logger.log_print(
                    f"event=worker_spawn worker_id={worker_id} print_to_console={print_to_console}",
                    module="pool_orchestrator",
                )

            if not active:
                break

            done, _ = await asyncio.wait(set(active), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                worker_id = active.pop(task)
                if self._console_owner == worker_id:
                    self._console_owner = None

                try:
                    result = task.result()
                except Exception as exc:
                    self.logger.log_print(
                        f"event=worker_failed worker_id={worker_id} error={exc}",
                        module="pool_orchestrator",
                        level="ERROR",
                    )
                    continue

                if result.status == "verified":
                    is_theorem = await self._check_is_theorem(
                        statement=result.lemma.get("statement", "")
                    )
                    result.is_theorem = is_theorem
                    result.lemma["is_theorem"] = is_theorem
                    self.logger.log_print(
                        f"event=orchestrator_check_is_theorem worker_id={worker_id} is_theorem={is_theorem}",
                        module="pool_orchestrator",
                    )

                decision = self.pool.commit(result)
                self.log_session.update_version()
                self.logger.log_print(
                    f"event=worker_finished worker_id={worker_id} status={decision.status} solved={decision.solved}",
                    module="pool_orchestrator",
                )

            if self.pool.is_solved() or self.pool.is_full():
                continue

        return PoolRunResult(solved=self.pool.is_solved(), summary=None)

    async def _run_one_worker(self, worker_id: int, print_to_console: bool):
        worker_logger = self.log_session.worker_logger(worker_id, print_to_console=print_to_console)
        worker = LemmaWorker(
            logger=worker_logger,
            execution_gateway=self.execution_gateway,
            print_to_console=print_to_console,
        )

        ctx = LemmaWorkerContext(
            problem=self.problem,
            hint=self.hint,
            verified_snapshot=self.pool.snapshot_verified(),
            remaining_capacity=self.pool.remaining_verified_capacity(),
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
