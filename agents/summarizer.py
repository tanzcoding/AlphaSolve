import time
import json

from utils.utils import extract_substring
from .shared_context import build_reasoning_path, save_snapshot
from config.agent_config import AlphaSolveConfig

from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '<conjecture>'
CONJECTURE_END = '</conjecture>'
PROOF_BEGIN = '<proof>'
PROOF_END = '</proof>'


def _format_lemmas_as_markdown(bundle):
    if not bundle:
        return "No verified lemmas were produced."

    lines = [
        "The research pipeline verified the following lemmas:",
    ]

    for entry in bundle:
        lemma_id = entry.get("id", "?")
        statement = (entry.get("statement") or "(statement missing)").strip()
        proof = (entry.get("proof") or "(proof missing)").strip()

        lines.append("")
        lines.append(f"### Lemma {lemma_id}")
        lines.append("")
        lines.append("**Statement**")
        lines.append(statement)
        lines.append("")
        lines.append("**Proof**")
        lines.append(proof)

    return "\n".join(lines).strip()

class Summarizer(Node):

    def __init__(self, problem, llm, prompt_file_path, logger): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Summarizer, self).__init__()
        self.logger = logger

    def prep(self,shared): 
        # READ ONLY from shared.
        self.logger.log_print('entering summarizer...', module='summarizer')

        lemma_id = shared["current_lemma_id"]
        if lemma_id is None:
            return AlphaSolveConfig.EXIT_ON_ERROR, None
        if shared["lemmas"][lemma_id].get("status") != "verified" or not shared["lemmas"][lemma_id].get("is_theorem", True):
            self.logger.log_print(
                "event=AlphaSolve failed to solve the problem, step=prep",
                module="summarizer",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_FAILURE, None

        # Include transitive dependencies + the final lemma, and output them in id order.
        ids = build_reasoning_path(shared["lemmas"], lemma_id, verified_only=False)
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
        return AlphaSolveConfig.NORMAL, _format_lemmas_as_markdown(bundle)

    def post(self, shared, prep_res, exec_res):
        # WRITE ONLY to shared.
        if not exec_res or len(exec_res) < 2:
            self.logger.log_print('exiting summarizer...', module='summarizer')
            save_snapshot(shared, "summarizer", "exit")
            return
        if exec_res[0] != AlphaSolveConfig.NORMAL:
            self.logger.log_print('exiting summarizer...', module='summarizer')
            save_snapshot(shared, "summarizer", "exit")
            return

        shared["result_summary"] = exec_res[1]
        self.logger.log_print(
            "event=summary_written step=post",
            module="summarizer",
        )
        self.logger.log_print('exiting summarizer...', module='summarizer')
        save_snapshot(shared, "summarizer", "completed")


def create_summarizer_agent(problem, prompt_file_path, logger: Logger):
 
    llm = LLMClient(module='summarizer', config=AlphaSolveConfig.SUMMARIZER_CONFIG, logger=logger)
    return Summarizer(problem, llm, prompt_file_path, logger=logger) 
