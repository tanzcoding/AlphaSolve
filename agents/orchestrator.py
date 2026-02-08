import json
from agents.shared_context import SharedContext, save_snapshot
from typing import Optional
from utils.utils import extract_substring, load_prompt_from_file
from llms.utils import LLMClient
from config.agent_config import AlphaSolveConfig
from .shared_context import *
from utils.logger import Logger


class Orchestrator:  

    ## 用来实现 kimi 的 agent swarm 模式, 当前做三件事: (1) 分析和打印引理图的结构; (2) 判断引理的相似性, 并去掉没有价值的引理; (3) 当探索收敛的时候, 开启新的引理池子
    ## 我们先来做 (1) 和(2)
    
    def __init__(self, llm, prompt_file_path, problem, lemma_pool, logger):
        self.llm = llm
        self.problem = problem
        self.lemma_pool = lemma_pool
        self.prompt_file_path = prompt_file_path

        self.logger = logger

    def generate_problem_and_hint(self):
        return self.problem, ''


def create_orchestrator_agent(problem, prompt_file_path, lemma_pool, logger):
    llm = LLMClient(module='orchestrator', config=AlphaSolveConfig.ORCHESTRATOR_CONFIG, logger=logger)
    return Orchestrator(llm, prompt_file_path, problem, lemma_pool, logger)        
