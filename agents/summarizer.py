import time
import json
import agents.conjecture_graph

from agents.utils import build_conjuecture_helper

from config.agent_config import AlphaSolveConfig

from llms.utils import LLMClient
from utils.logger import log_print

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'

class Summarizer(Node):

    def __init__(self, problem, llm, prompt_file_path, print_to_console): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Summarizer, self).__init__()
        self.print_to_console = print_to_console

    def prep(self,shared): 
        return None

    def exec(self,prep_res): 
        return None

    def post(self, shared, prep_res, exec_res): 
        result_map = {}

        result_map['conjucture'] = shared[AlphaSolveConfig.CURRENT_CONJECTURE].conjecture
        result_map['proof'] = shared[AlphaSolveConfig.CURRENT_CONJECTURE].proof
 
        shared[AlphaSolveConfig.RESULT_SUMMARY] = json.dumps(result_map)

        log_print('[summarizer] summarization done ...', print_to_console=self.print_to_console)


def create_summarizer_agent(problem, prompt_file_path, print_to_console):
 
    llm = LLMClient(AlphaSolveConfig.SUMMARIZER_CONFIG)
    return Summarizer(problem, llm, prompt_file_path, print_to_console) 
