import time
import json
import agents.conjecture_graph

from agents.utils import build_conjuecture_helper
from llms.kimi import KimiClient
from llms.deepseek import DeepSeekClient
from config.agent_config import AlphaSolveConfig

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'

class Summarizer(Node):

    def __init__(self, problem, llm, model, prompt_file_path): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Summarizer, self).__init__()

    def prep(self,shared): 
        pass

    def exec(self,prep_res): 
        pass

    def post(self, shared, prep_res, exec_res): 
        pass
 


def create_summarizer_agent(problem, model, prompt_file_path):
 
    ds = DeepSeekClient()
    return Summarizer(problem, ds,  model, prompt_file_path) 
