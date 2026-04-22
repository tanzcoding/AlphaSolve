from __future__ import annotations

import asyncio
from typing import Optional

from alphasolve.agents.lemma_pool import LemmaPool
from alphasolve.agents.pool_orchestrator import LemmaPoolOrchestrator
from alphasolve.agents.summarizer import create_summarizer_agent
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.execution import ExecutionGateway
from alphasolve.utils.log_session import LogSession
from alphasolve.utils.logger import Logger


class AlphaSolve:
    def __init__(
        self,
        problem: str,
        print_to_console: bool = True,
        tool_executor_size: int = 2,
        log_session: Optional[LogSession] = None,
        logger: Optional[Logger] = None,
        init_from_previous: bool = True, 
    ):
        self.problem = problem
        self.max_worker_num = max(AlphaSolveConfig.MAX_WORKER_NUM, 1)
        self.print_to_console = print_to_console
        self.log_session = log_session or LogSession(run_root=AlphaSolveConfig.LOG_PATH, progress_path = AlphaSolveConfig.PROGRESS_PATH)
        self.logger = logger or self.log_session.main_logger(print_to_console=print_to_console)
        self.execution_gateway = ExecutionGateway(
            python_workers=max(1, int(tool_executor_size)),
            wolfram_enabled=AlphaSolveConfig.WOLFRAM_AVAILABLE,
            logger=self.logger,
        )
        self.init_from_previous = init_from_previous
        # 记录各个子代理使用的模型信息
        self._log_model_configs()

    def _log_model_configs(self):
        self.logger.log_section("AlphaSolve 各子代理模型配置", width=60)
        self.logger.log_print(f"Generator:         {AlphaSolveConfig.GENERATOR_CONFIG.get('model', 'N/A')}")
        self.logger.log_print(f"Verifier:          {AlphaSolveConfig.VERIFIER_CONFIG.get('model', 'N/A')}")
        self.logger.log_print(f"Reviser:           {AlphaSolveConfig.REVISER_CONFIG.get('model', 'N/A')}")
        self.logger.log_print(f"Proof Subagent:    {AlphaSolveConfig.PROOF_SUBAGENT_CONFIG.get('model', 'N/A')}")
        self.logger.log_print(f"Compute Subagent:  {AlphaSolveConfig.COMPUTE_SUBAGENT_CONFIG.get('model', 'N/A')}")
        self.logger.log_separator('section', width=60)
        self.logger.log_print("")

    def do_research(self):
        return asyncio.run(self.do_research_async())

    async def do_research_async(self):
        last_summary = None

        version = self.log_session.previous_state_path()

        self.logger.log_print(f"event=alphasolve_start with version: {version}", module="AlphaSolve")

        pool = LemmaPool(
            capacity_verified = AlphaSolveConfig.MAX_LEMMA_NUM,
            logger = self.logger,
            snapshot_path = self.log_session.pool_state_path(pool_id=0),
            previous_snapshot_path = version,
            init_from_previous = self.init_from_previous,
        )

        problem_text, hint = self.generate_problem_and_hint()

        orchestrator = LemmaPoolOrchestrator(
            pool = pool,
            logger = self.logger,
            log_session = self.log_session,
            problem = problem_text,
            hint = hint,
            execution_gateway = self.execution_gateway,
            parallelism_limit = self.max_worker_num,
            print_to_console = self.print_to_console,
            
        )
        run_result = await orchestrator.run_async()

        if run_result.solved:
            summary = self._summarize_solution(problem_text, pool)
            if summary:
                return summary
            last_summary = summary
        else:
            self.logger.log_print("event=pool_not_solved", module="AlphaSolve", level="WARNING")

        return last_summary

    def _summarize_solution(self, problem: str, pool: LemmaPool):
        lemmas = pool.snapshot_all()
        theorem_id = None
        for i, lemma in enumerate(lemmas):
            if lemma.get("status") == "verified" and lemma.get("is_theorem"):
                theorem_id = i

        if theorem_id is None:
            return None

        shared_context = {
            "problem": problem,
            "hint": None,
            "lemmas": lemmas,
            "current_lemma_id": theorem_id,
            "result_summary": None,
        }

        summarizer = create_summarizer_agent(
            problem=problem,
            prompt_file_path=AlphaSolveConfig.SUMMARIZER_PROMPT_PATH,
            logger=self.logger,
        )
        prep_res = summarizer.prep(shared_context)
        exec_res = summarizer.exec(prep_res)
        summarizer.post(shared_context, prep_res, exec_res)
        return shared_context.get("result_summary")

    def do_close(self):
        try:
            if self.execution_gateway is not None:
                self.execution_gateway.close()
        except Exception:
            pass

    def generate_problem_and_hint(self):
        return self.problem, None


if __name__ == "__main__":
    alpha = AlphaSolve("", AlphaSolveConfig.MAX_WORKER_NUM)
    alpha.do_research(1)

