import time
import json
from pocketflow import Node
from typing import Any, Dict, List, Optional

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger
from utils.utils import load_prompt_from_file
from .shared_context import SharedContext, build_reasoning_path


class NoHistoryRefiner(Node):
    """A refiner that does NOT expose solver history; it only sees the latest lemma state + review."""

    def __init__(self, prompt_file_path: str, llm: LLMClient, logger: Logger):
        super(NoHistoryRefiner, self).__init__()
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm
        self.logger = logger

    def prep(self, shared: SharedContext):
        # READ ONLY from shared here.
        self.logger.log_print('entering refiner...', module='search_refiner')

        lemma_id = shared.get("current_lemma_id")
        if lemma_id is None:
            self.logger.log_print(
                "event=no_current_lemma step=prep",
                module="search_refiner",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR, None, None

        if shared["lemmas"][lemma_id].get("verify_round", 0) >= AlphaSolveConfig.MAX_VERIFY_AND_REFINE_ROUND:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, None, None

        lemma = shared["lemmas"][lemma_id]
        ctx_ids = build_reasoning_path(shared["lemmas"],lemma_id, verified_only=True)
        ctx_text = self.__render_context(ctx_ids, shared["lemmas"])

        prompt = self.__build_refiner_prompt(lemma, ctx_text)

        self.logger.log_print(
            f"event=context_built step=prep lemma_id={lemma_id} ctx_size={len(ctx_ids)}",
            module="refiner",
            print_to_console=self.print_to_console,
        )
        return AlphaSolveConfig.NORMAL, prompt, shared

    def exec(self, prep_res):
        if prep_res[0] == AlphaSolveConfig.VERIFIER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED
        
        prompt = prep_res[1]
        shared = prep_res[2]

        messages_to_send = [
            {"role": "user", "content": prompt}
        ]

        _, _, _ = self.llm.get_result(messages_to_send, shared=shared)

        return AlphaSolveConfig.NORMAL

    def post(self, shared: SharedContext, prep_res, exec_res):
        if prep_res[0] == AlphaSolveConfig.VERIFIER_EXAUSTED:
            # verify-refine quota exhausted: we are abandoning the current lemma.
            # Bugfix: refund the solver lemma quota when the current lemma never
            # becomes verified after all verify-refine attempts.
            # In the post function, we should get lemma_id like this:
            lemma_id = shared.get("current_lemma_id")
            if lemma_id is not None and 0 <= lemma_id < len(shared.get("lemmas", [])):
                lemma = shared["lemmas"][lemma_id]
                # Mark rejected so it won't be reused as context.
                lemma["status"] = "rejected"
                self.logger.log_print(
                    f"event=conjecture rejected and would not be refined again, step=post, lemma_id={lemma_id}",
                    module="refiner",
                    level="WARNING",
                )
            self.logger.log_print('exiting refiner...', module='refiner')
            return AlphaSolveConfig.EXIT_ON_EXAUSTED
        return AlphaSolveConfig.REFINE_SUCCESS
    
    def __render_context(self, ctx_ids, lemmas):
        if not ctx_ids:
            return None
        lines = []
        lines.append("## Context and History Explorations")
        lines.append("")
        lines.append(
            "Here is a list of context that we have collected for this problem or our history findings during exploration. "
            "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
        )
        lines.append("")
        for i, lemma_id in enumerate(ctx_ids):
            lines.append(f" ** Conjecture-{i} **")
            lines.append(f" {lemmas[lemma_id].get('statement')}")
        return "\n".join(lines)
    
    def __build_refiner_prompt(self, lemma, reasoning_ctx): ## 把所有东西拼到 prompt 里

        if not lemma.get("statement") or not lemma.get("proof"):
            return None

        tmp = self.prompt_template.replace('{conjecture_content}', lemma.get("statement", "")).replace('{proof_content}', lemma.get("proof", ""))

        if lemma.get("review"):
            tmp = tmp.replace('{review_content}', lemma.get("review"))

        if reasoning_ctx:
            tmp = tmp + '\n' + reasoning_ctx

        return tmp

def shared_instruction_block(lemma, shared: SharedContext) -> str:
    from utils.utils import load_prompt_from_file
    prompt_template = load_prompt_from_file(AlphaSolveConfig.SEARCH_REFINER_PROMPT_PATH)
    ctx_text = build_dependent_section(shared)
    return (
        prompt_template
        .replace("{conjecture_content}", lemma.get("statement", ""))
        .replace("{proof_content}", lemma.get("proof", ""))
        .replace("{review_content}", lemma.get("review", ""))
        .replace("{dependent_lemmas_section}", ctx_text or "")
    )


def build_dependent_section(shared: SharedContext) -> str:
    from .shared_context import build_reasoning_path
    lemma_id = shared.get("current_lemma_id")
    if lemma_id is None:
        return ""
    ctx_ids = build_reasoning_path(shared["lemmas"], lemma_id, verified_only=True)
    if not ctx_ids:
        return ""
    lines = ["### Dependent Lemmas", ""]
    for offset, cid in enumerate(ctx_ids):
        lemma = shared['lemmas'][cid]
        lines.append(f"Lemma {cid}: {lemma.get('statement', '').strip()}")
    return "\n".join(lines)


def create_no_history_refiner_agent(prompt_file_path: str, logger: Logger):
    llm = LLMClient(module='no_history_refiner', config=AlphaSolveConfig.NO_HISTORY_REFINER_CONFIG, logger=logger)
    return NoHistoryRefiner(prompt_file_path=prompt_file_path, llm=llm, logger=logger)
