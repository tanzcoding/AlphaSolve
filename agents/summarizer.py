import time
import json

from agents.utils import build_conjecture_helper

from config.agent_config import AlphaSolveConfig

from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'

class Summarizer(Node):

    def __init__(self, problem, llm, prompt_file_path, logger): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Summarizer, self).__init__()
        self.logger = logger

    def prep(self,shared): 
        # READ ONLY from shared.
        lemma_id = shared["current_lemma_id"]
        if lemma_id is None:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        # Include transitive dependencies + the final lemma, and output them in id order.
        ids = shared.build_reasoning_path(lemma_id, verified_only=False)
        ids.append(lemma_id)
        ids = sorted(set(ids))

        lemmas = shared["lemmas"]
        bundle = []
        for i in ids:
            l = lemmas[i]
            bundle.append(
                {
                    "id": i,
                    "statement": l.get("statement"),
                    "proof": l.get("proof"),
                }
            )
        return AlphaSolveConfig.NORMAL, bundle

    def exec(self,prep_res): 
        if not prep_res or len(prep_res) < 2:
            return AlphaSolveConfig.EXIT_ON_ERROR, None
        if prep_res[0] != AlphaSolveConfig.NORMAL:
            return AlphaSolveConfig.EXIT_ON_ERROR, None
        bundle = prep_res[1]
        return AlphaSolveConfig.NORMAL, json.dumps({"lemmas": bundle}, ensure_ascii=False, indent=2)

    def post(self, shared, prep_res, exec_res): 
        # WRITE ONLY to shared.
        if not exec_res or len(exec_res) < 2:
            return
        if exec_res[0] != AlphaSolveConfig.NORMAL:
            return

        shared["result_summary"] = exec_res[1]
        self.logger.log_print(
            "event=summary_written step=post",
            module="summarizer",
        )


def create_summarizer_agent(problem, prompt_file_path, logger: Logger):
 
    llm = LLMClient(AlphaSolveConfig.SUMMARIZER_CONFIG, logger=logger)
    return Summarizer(problem, llm, prompt_file_path, logger=logger) 
