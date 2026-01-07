import os, time, threading, random

from config.agent_config import AlphaSolveConfig
from agents.shared_context import new_shared_context

from agents.solver import create_solver_agent
from agents.verifier import create_verifier_agent
from agents.diff_refiner import create_diff_refiner_agent
from agents.refiner import create_refiner_agent
from agents.summarizer import create_summarizer_agent

from pocketflow import Flow

from utils.logger import Logger

class AlphaSolve:

    ## 首先, AIM 其实没什么东西可以复用的,整体上目前大部分Math Aget 都可以总结成, solve(explorer)-verify(reviewer)-refine 的模式
    ## 但是具体做看实验了, 因此 AlphaSolve 的主类仅封装: (1) solve(explorer)-verify(reviewer)-refine 的模式 (2) solve & verify & refine 的历史 trace

    def __init__(self, problem, print_to_console = False):
        self.problem = problem
        self.logger = Logger(log_dir=AlphaSolveConfig.LOG_PATH,print_to_console=print_to_console)

        # shared is a single SharedContext(dict) instance.
        # All nodes read only in prep and write only in post.
        self.shared = new_shared_context(
            problem=self.problem,
            hint=None,
        )


    def __create_research_flow(self):  ## 主类入口

        self.logger.log_print(
            '[AlphaSolve] create solver node, using model ',
            AlphaSolveConfig.SOLVER_CONFIG['model'],
            ' and prompt path ',
            AlphaSolveConfig.SOLVER_PROMPT_PATH,
            module='alphasolve',
        )
        solver = create_solver_agent(problem=self.problem, prompt_file_path=AlphaSolveConfig.SOLVER_PROMPT_PATH, logger=self.logger)

        self.logger.log_print(
            '[AlphaSolve] create verifier node, using model ',
            AlphaSolveConfig.VERIFIER_CONFIG['model'],
            ' and prompt path ',
            AlphaSolveConfig.VERIFIER_PROMPT_PATH,
            module='alphasolve',
        )
        verifier = create_verifier_agent(problem=self.problem, prompt_file_path=AlphaSolveConfig.VERIFIER_PROMPT_PATH, logger=self.logger)
       
        refiner = create_refiner_agent(logger=self.logger)
     
        summarizer = create_summarizer_agent(problem=self.problem, prompt_file_path=AlphaSolveConfig.SUMMARIZER_PROMPT_PATH, logger=self.logger)

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
  
    def do_research(self):

        flow = self.__create_research_flow()
        flow.run(self.shared)

        ## 走到这里就说明
        try:
            # New schema: result is stored directly on shared.
            result = self.shared["result_summary"]

            self.logger.log_print('alpha solve result is: ', result, module='alphasolve')

            return result
        except KeyError:
            self.logger.log_print('error execute on alpha solve, no summary', module='alphasolve', level='ERROR')
            return None



