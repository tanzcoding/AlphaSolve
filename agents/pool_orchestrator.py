from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import Optional

from agents.lemma_pool import LemmaPool
from agents.lemma_worker import LemmaWorker
from agents.lemmaworker import LemmaWorkerContext
from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient, ParallelLLMClient
from utils.log_session import LogSession
from utils.logger import Logger


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
        tool_executor,
        parallelism_limit: int,
    ):
        self.pool = pool
        self.logger = logger
        self.log_session = log_session
        self.problem = problem
        self.hint = hint
        self.tool_executor = tool_executor
        self.parallelism_limit = max(1, int(parallelism_limit))
        self._next_worker_id = 0
        self._console_owner: Optional[int] = None
        
        # Initialize LLM client for is_theorem checking
        if not tool_executor:
            self.llm = LLMClient(module='orchestrator', config=AlphaSolveConfig.ORCHESTRATOR_CONFIG, logger=logger)
        else:
            self.llm = ParallelLLMClient(module='orchestrator', config=AlphaSolveConfig.ORCHESTRATOR_CONFIG, logger=logger, tool_executor=tool_executor)

    def run(self) -> PoolRunResult:
        self.logger.log_print(
            f"event=pool_start parallelism_limit={self.parallelism_limit} capacity_verified={self.pool.capacity_verified}",
            module="pool_orchestrator",
        )

        active: dict = {}
        with ThreadPoolExecutor(max_workers=self.parallelism_limit) as executor:
            while True:
                while (not self.pool.is_solved()) and (not self.pool.is_full()) and len(active) < self.parallelism_limit:
                    worker_id = self._next_worker_id
                    self._next_worker_id += 1

                    if self._console_owner is None:
                        self._console_owner = worker_id
                    print_to_console = self._console_owner == worker_id

                    future = executor.submit(self._run_one_worker, worker_id, print_to_console)
                    active[future] = worker_id
                    self.logger.log_print(
                        f"event=worker_spawn worker_id={worker_id} print_to_console={print_to_console}",
                        module="pool_orchestrator",
                    )

                if not active:
                    break

                done, _ = wait(set(active.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    worker_id = active.pop(fut)
                    if self._console_owner == worker_id:
                        self._console_owner = None

                    try:
                        result = fut.result()
                    except Exception as exc:
                        self.logger.log_print(
                            f"event=worker_failed worker_id={worker_id} error={exc}",
                            module="pool_orchestrator",
                            level="ERROR",
                        )
                        continue

                    # Check if this lemma is a theorem (only if it's verified)
                    if result.status == "verified":
                        is_theorem = self._check_is_theorem(statement=result.lemma.get("statement", ""))
                        result.is_theorem = is_theorem
                        result.lemma["is_theorem"] = is_theorem
                        self.logger.log_print(
                            f"event=orchestrator_check_is_theorem worker_id={worker_id} is_theorem={is_theorem}",
                            module="pool_orchestrator",
                        )

                    ## 一旦调用 commit, 我们就必须更新 current_version 了, 确保下一次 load 能读到最新的
                    decision = self.pool.commit(result)
                    self.log_session.update_version()

                    self.logger.log_print(
                        f"event=worker_finished worker_id={worker_id} status={decision.status} solved={decision.solved}",
                        module="pool_orchestrator",
                    )

                if self.pool.is_solved() or self.pool.is_full():
                    continue

        return PoolRunResult(solved=self.pool.is_solved(), summary=None)

    def _run_one_worker(self, worker_id: int, print_to_console: bool):
        worker_logger = self.log_session.worker_logger(worker_id, print_to_console=print_to_console)
        worker = LemmaWorker(
            logger=worker_logger,
            tool_executor=self.tool_executor,
            print_to_console=print_to_console,
        )

        ## 用临时变量存一下
        verified_lemmas = self.pool.snapshot_verified()
        remaining_capacity = self.pool.remaining_verified_capacity()

        ctx = LemmaWorkerContext(
            problem=self.problem,
            hint=self.hint,
            verified_snapshot=verified_lemmas, 
            remaining_capacity=remaining_capacity,
            run_id=self.log_session.run_id,
            worker_id=worker_id,
        )
        return worker.run(ctx)

    def _check_is_theorem(self, *, statement: str) -> bool:
        """Check if a lemma statement fully addresses the original problem."""

        ## 用临时变量存一下
        verified_lemmas = self.pool.snapshot_verified()

        shared = {
            "problem": self.problem,
            "hint": self.hint,
            "lemmas": verified_lemmas,
            "current_lemma_id": None,
            "result_summary": None,
        }
        
        is_theorem_num = 0
        for _ in range(AlphaSolveConfig.CHECK_IS_THEOREM_TIMES):
            check_message = (
                "Check if the following statement fully addresses the problem "
                "(do NOT check if the statement is mathematically correct - only check if it fully resolves the problem). "
                "Output ONLY 'Yes' or 'No' without any explanation.\n\n"
                f"Problem: {self.problem}\n\nStatement: {statement}"
            )
            response, _, _ = self.llm.get_result(messages=[{"role": "user", "content": check_message}], tools=[], shared=shared)
            if response.strip().lower() == 'yes':
                is_theorem_num += 1
            else:
                return False
        return is_theorem_num >= AlphaSolveConfig.CHECK_IS_THEOREM_TIMES

