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
import threading
import time
import threading
import schedule
from datetime import datetime



from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed


def _do_research(problem, hint, lemma_pool, print_to_console, iteration_round, mode, t_suffix, tool_executor):

    ## init logger for every process
    tid = str(threading.get_ident())
    name = 'alpha_solve' + '_' + tid

    dir_path = AlphaSolveConfig.LOG_PATH + '/alpha_solve_iteration_'  + str(iteration_round) +  '_' + t_suffix

    print('create logger for ', name, 'with dir ', dir_path)

    logger = Logger(log_dir = dir_path, name = name, print_to_console = print_to_console)

    shared_context = new_shared_context(
        problem = problem,
        hint = hint,
        lemma_pool = lemma_pool,
        iteration = iteration_round,
        mode = mode
    )

    flow = _create_research_flow(problem, hint, logger, tool_executor)
    flow.run(shared_context)

    try:
        # New schema: result is stored directly on shared.
        result = shared_context["result_summary"]

        logger.log_print('AlphaSolve result is: ', result, module='AlphaSolve')

        return result

    except KeyError:
       
        logger.log_print('error execute on AlphaSolve, no summary', module='AlphaSolve', level='ERROR')
        return None


def _create_research_flow(problem, hint, logger, tool_executor):  

    logger.log_print('create solver node, using model ', AlphaSolveConfig.SOLVER_CONFIG['model'], ' and prompt path ', AlphaSolveConfig.SOLVER_PROMPT_PATH, module='AlphaSolve',)

    solver = create_solver_agent(
        prompt_file_path = AlphaSolveConfig.SOLVER_PROMPT_PATH,
        logger = logger,
        tool_executor = tool_executor

    )

    logger.log_print('create verifier node, using model ', AlphaSolveConfig.VERIFIER_CONFIG['model'], ' and prompt path ', AlphaSolveConfig.VERIFIER_PROMPT_PATH, module = 'AlphaSolve',)

    verifier = create_verifier_agent(
        prompt_file_path=AlphaSolveConfig.VERIFIER_PROMPT_PATH,
        logger=logger,
        tool_executor = tool_executor
    )

    logger.log_print('create refiner node, using model ',  AlphaSolveConfig.REFINER_CONFIG['model'],  ' and prompt path ', AlphaSolveConfig.REFINER_PROMPT_PATH, module='AlphaSolve',)

    refiner = create_refiner_agent(
        prompt_file_path = AlphaSolveConfig.REFINER_PROMPT_PATH, 
        logger = logger,
        tool_executor = tool_executor
    )

    summarizer = create_summarizer_agent(
        problem =problem, 
        prompt_file_path = AlphaSolveConfig.SUMMARIZER_PROMPT_PATH, 
        logger = logger
    )

    
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

    def __init__(self, problem, max_worker_num, print_to_console = True, mode = AlphaSolveConfig.SHARED_BY_ALL, 
        tool_executor_size = 2):

        self.problem = problem
        self.logger = Logger(log_dir=AlphaSolveConfig.LOG_PATH, name = 'main', print_to_console=print_to_console)

        self.executor = ThreadPoolExecutor(max_workers = max_worker_num)
        self.max_worker_num = max_worker_num
        self.tool_executor = ProcessPoolExecutor(max_workers = tool_executor_size)
        self.t_suffix = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        self.lemma_pool = [ ]
        self.orchestrator = create_orchestrator_agent(problem = self.problem, 
            prompt_file_path = AlphaSolveConfig.ORCHESTRATOR_PROMPT_PATH,
            lemma_pool = self.lemma_pool, 
            logger = self.logger
        )

        self.mode = mode
        self.tool_executor_size = tool_executor_size


        def _monitor():

            while True:
                self._check_lemma_pool()
                time.sleep(60)

        self.monitor = threading.Thread(target = _monitor).start()


    def _check_lemma_pool(self):

        self.logger.log_print('lemma pool checking thread started ...', module='AlphaSolve')

        if len(self.lemma_pool) > 0:
            self.logger.log_print('lemma pool with length ', len(self.lemma_pool), module='AlphaSolve')
            for i in range(self.lemma_pool):
                lemma = self.lemma_pool[i]
                try:
                    statement = lemma['statement']
                    status = lemma['status']
                    verify_round = lemma['verify_round']
                    is_theorem = lemma['is_theorem']
  
                    self.logger.log_print('============ begin with lemma index ', i,  module='AlphaSolve')
                    self.logger.log_print('lemma statement ', statement,  module='AlphaSolve')
                    self.logger.log_print('lemma status ', status,  module='AlphaSolve')
                    self.logger.log_print('lemma status ', verify_round,  module='AlphaSolve')
                    self.logger.log_print('lemma status ', is_theorem,  module='AlphaSolve')
                    self.logger.log_print('============= end with lemma index ', i,  module='AlphaSolve')
                except KeyError as ke:
                    traceback.print_exc()

        else: ## 还没有产生 lemma
            self.logger.log_print('lemma pool is empty', module='AlphaSolve')


        self.logger.log_print('lemma pool checking thread done, sleep ...', module='AlphaSolve')
       

    def do_research(self, iteration_num = 1):

        for k in range(iteration_num):

            self.logger.log_print('alphasolve run for iteration ', k, module='AlphaSolve') 

            self.prepare_lemma_pool()
            futures = [ ] 

            ## 随机选择一个进程打印到 console
            index = random.randint(0, self.max_worker_num) 
            self.logger.log_print('choose index for log printing ', index, module='AlphaSolve')

            for i in range(self.max_worker_num):
                self.logger.log_print('alphasolve run for batch ', i, module='AlphaSolve')
                problem_text, hint = self.generate_problem_and_hint()

                future = self.executor.submit(_do_research, problem_text, hint, self.lemma_pool, i == index, k, self.mode, self.t_suffix, self.tool_executor)
                futures.append(future)

            finished = 0

            for fut in as_completed(futures): 

                try:
                    data = fut.result()
                    if data and len(data) > 0:  ## 已经有结果了, 直接返回
                        return data

                    self.logger.log_print('finished num ', finished, module='AlphaSolve')

                except Exception as exc:
                    traceback.print_exc()

                finished += 1

        ## 全部跑完了, 没有结果, 返回 None 
        return None


    def do_close(self): ## 关闭掉整个 AlphaSolve

        self.executor.shutdown(wait = True)

        try:
            self.manager.shutdown()
        except Exception:
            pass

        self.monitor.join()

    def generate_problem_and_hint(self): ## 给orchestrator留的口子, 后续可以肆意生成 problem 和 hint
        return self.orchestrator.generate_problem_and_hint()

    
    def prepare_lemma_pool(self): ## 给orchestrator留的口子, 后续可以在两个 iteration 中间更新/修正/处理 lemma pool, 比如扔掉一些重复的 lemma, 很飞的 lemma
        pass


if __name__== "__main__" :
    alpha = AlphaSolve('', 2)
    alpha.do_research(2, 1)
