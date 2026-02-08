import os, time, threading, random

from config.agent_config import AlphaSolveConfig
from agents.shared_context import new_shared_context

from agents.solver import create_solver_agent
from agents.verifier import create_verifier_agent
from agents.refiner import create_refiner_agent
from agents.no_history_refiner import create_no_history_refiner_agent
from agents.orchestrator import create_orchestrator_agent
from agents.summarizer import create_summarizer_agent
from pocketflow import Flow
from utils.logger import Logger

import traceback
import multiprocessing as mp
import os
import random

from concurrent.futures import ProcessPoolExecutor, as_completed

def _do_research(problem, hint, lemma_pool, print_to_console, iteration_round, mode):

    ## init logger for every process
    process_id = str(os.getpid())
    name = 'AlphaSolve' + '_' + process_id

    logger = Logger(log_dir=AlphaSolveConfig.LOG_PATH, name = name, print_to_console=print_to_console)

    shared_context = new_shared_context(
        problem = problem,
        hint = hint,
        lemma_pool = lemma_pool,
        iteration = iteration_round,
        mode = mode
    )

    flow = _create_research_flow(problem, hint, lemma_pool, logger)
    flow.run(shared_context)

    try:
        # New schema: result is stored directly on shared.
        result = shared_context["result_summary"]

        logger.log_print('AlphaSolve result is: ', result, module='AlphaSolve')

        return result

    except KeyError:
       
        logger.log_print('error execute on AlphaSolve, no summary', module='AlphaSolve', level='ERROR')
        return None


def _create_research_flow(problem, hint, lemma_pool, logger):  ## 主入口, 移出来了, 因为 self.xxx 多进程无法调用

    logger.log_print('create solver node, using model ', AlphaSolveConfig.SOLVER_CONFIG['model'], ' and prompt path ', AlphaSolveConfig.SOLVER_PROMPT_PATH, module='AlphaSolve',)

    solver = create_solver_agent(
        prompt_file_path = AlphaSolveConfig.SOLVER_PROMPT_PATH,
        logger = logger)

    logger.log_print('create verifier node, using model ', AlphaSolveConfig.VERIFIER_CONFIG['model'], ' and prompt path ', AlphaSolveConfig.VERIFIER_PROMPT_PATH, module = 'AlphaSolve',)

    verifier = create_verifier_agent(
        prompt_file_path=AlphaSolveConfig.VERIFIER_PROMPT_PATH,
        logger=logger)

    logger.log_print('create refiner node, using model ',  AlphaSolveConfig.REFINER_CONFIG['model'],  ' and prompt path ', AlphaSolveConfig.REFINER_PROMPT_PATH, module='AlphaSolve',)

    refiner = create_refiner_agent(prompt_file_path=AlphaSolveConfig.REFINER_PROMPT_PATH, logger = logger)

    summarizer = create_summarizer_agent(problem=problem, prompt_file_path = AlphaSolveConfig.SUMMARIZER_PROMPT_PATH, logger = logger)

    
    ## 成功生成 lemma, 下一站去 verifier
    solver - AlphaSolveConfig.CONJECTURE_GENERATED >> verifier
     
    ## 错误, 回到 solver 重试
    solver - AlphaSolveConfig.EXIT_ON_ERROR >> solver
    
    ## 轮次打满, 给 summarizer 总结, 退出
    solver - AlphaSolveConfig.EXIT_ON_EXAUSTED >> summarizer
        
    ## 发现错误, 去改
    verifier - AlphaSolveConfig.CONJECTURE_UNVERIFIED >> refiner
    
    ## 正确, 给到 solver
    verifier - AlphaSolveConfig.CONJECTURE_VERIFIED >> solver
    
    ## 完成 theorem, 给 summarizer 总结, 退出
    verifier - AlphaSolveConfig.DONE >> summarizer

    ## verifier-refine 打满, 给 solver
    refiner - AlphaSolveConfig.EXIT_ON_EXAUSTED >> solver
    
    ## 改了, 回到 verifier
    refiner - AlphaSolveConfig.REFINE_SUCCESS >> verifier
    
    ## conj 是错的, 直接到 solver
    refiner - AlphaSolveConfig.CONJECTURE_WRONG >> solver
    
    ## 容错, 重新尝试
    refiner - AlphaSolveConfig.EXIT_ON_ERROR >> refiner

    return Flow(start = solver)


class AlphaSolve:

    ## 首先, AIM 其实没什么东西可以复用的,整体上目前大部分Math Aget 都可以总结成, solve(explorer)-verify(reviewer)-refine 的模式
    ## 但是具体做看实验了, 因此 AlphaSolve 的主类仅封装: (1) solve(explorer)-verify(reviewer)-refine 的模式 (2) solve & verify & refine 的历史 trace

    def __init__(self, problem, max_worker_num, print_to_console = True, mode = AlphaSolveConfig.SHARED_BY_ALL):

        self.problem = problem
        self.logger = Logger(log_dir=AlphaSolveConfig.LOG_PATH, name = 'main', print_to_console=print_to_console)

        self.manager = mp.Manager()
        self.executor = ProcessPoolExecutor(max_workers=max_worker_num, mp_context=mp.get_context("spawn"))

        self.lemma_pool = self.manager.list() 
        self.orchestrator = create_orchestrator_agent(problem = self.problem, 
            prompt_file_path = AlphaSolveConfig.ORCHESTRATOR_PROMPT_PATH,
            lemma_pool = self.lemma_pool, 
            logger = self.logger
        )

        self.mode = mode
       

    def do_research(self, batch_size, iteration_num = 1):

        for k in range(iteration_num):

            self.logger.log_print('alphasolve run for iteration ', (k + 1), module='AlphaSolve') 
            futures = [ ] 

            ## 随机选择一个进程打印到 console
            index = random.randint(0, batch_size) 
            self.logger.log_print('choose index for log printing ', index, module='AlphaSolve')

            for i in range(batch_size):
                self.logger.log_print('alphasolve run for batch ', (i + 1), module='AlphaSolve')
                problem_text, hint = self.generate_problem_and_hint()

                future = self.executor.submit(_do_research, problem_text, hint, self.lemma_pool, i == index, k, self.mode)
                futures.append(future)

            finished = 0

            for fut in as_completed(futures): 

                try:
                    data = fut.result()
                    if not data:  ## 已经有结果了, 直接返回
                        return data

                    results.append(data)
                    self.logger.log_print('finished num ', finished, module='AlphaSolve')

                except Exception as exc:
                    traceback.print_exc()

                finished += 1


        ## 全部跑完了, 没有结果, 返回 None 
        return None


    def do_close(self):

        self.executor.shutdown(wait=True)
        try:
            self.manager.shutdown()
        except Exception:
            pass

    def generate_problem_and_hint(self): ## 给orchestrator留的口子, 后续可以肆意生成 problem 和 hint
        return self.orchestrator.generate_problem_and_hint()


if __name__== "__main__" :
    alpha = AlphaSolve('', 2)
    alpha.do_research(2, 1)
