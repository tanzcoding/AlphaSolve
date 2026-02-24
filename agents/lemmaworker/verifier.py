from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List

from agents.shared_context import Lemma
from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient, ParallelLLMClient
from utils.logger import Logger
from utils.utils import load_prompt_from_file


VERIFY_RESULT_VALID = 'boxed{valid}'


@dataclass
class VerifyInput:
    problem: str
    verified_context: List[Lemma]
    candidate_lemma: Lemma


@dataclass
class VerifyOutput:
    valid: bool
    review: str
    cot: str
    done: bool


class Verifier:
    def __init__(self, llm: LLMClient, prompt_file_path: str, logger: Logger):
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def verify(self, input: VerifyInput) -> VerifyOutput:
        reasoning_ctx = self._render_context(input.verified_context)
        result = []
        verifier_res = (False, "", "")

        shared = {
            "problem": input.problem,
            "hint": None,
            "lemmas": input.verified_context,
            "current_lemma_id": None,
            "result_summary": None,
        }

        for _ in range(AlphaSolveConfig.VERIFIER_SCALING_FACTOR):
            is_valid, review, cot = self._verify_once(input.candidate_lemma, reasoning_ctx, shared)
            if not is_valid:
                result.append((is_valid, review, cot))
                break
            verifier_res = (is_valid, review, cot)

        if result:
            index = random.randrange(len(result)) if len(result) > 1 else 0
            is_valid, review, cot = result[index]
        else:
            is_valid, review, cot = verifier_res

        return VerifyOutput(
            valid=bool(is_valid),
            review=review,
            cot=cot,
            done=bool(is_valid and input.candidate_lemma.get("is_theorem")),
        )

    def _verify_once(self, lemma: Lemma, reasoning_ctx: str, shared: dict):
        prompt = self.prompt_template.replace('{conjecture_content}', lemma.get('statement', '')).replace('{proof_content}', lemma.get('proof', ''))
        if reasoning_ctx:
            prompt = prompt + '\n' + reasoning_ctx

        messages_to_send = [{"role": "user", "content": prompt}]
        answer, cot, _ = self.llm.get_result(messages_to_send, tools=AlphaSolveConfig.VERIFIER_CONFIG['tools'], shared=shared)
        return (VERIFY_RESULT_VALID in answer), answer, cot

    def _render_context(self, lemmas: List[Lemma]) -> str:
        if not lemmas:
            return ""
        lines = [
            "## Context and History Explorations",
            "",
            "Here is a list of context that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.",
            "",
        ]
        for i, lemma in enumerate(lemmas):
            lines.append(f" ** Lemma-{i} **")
            lines.append(f" {lemma.get('statement')}")
        return "\n".join(lines)


def create_verifier_component(prompt_file_path: str, logger: Logger, tool_executor=None) -> Verifier:
    if not tool_executor:
        llm = LLMClient(module='verifier', config=AlphaSolveConfig.VERIFIER_CONFIG, logger=logger)
    else:
        llm = ParallelLLMClient(module='verifier', config=AlphaSolveConfig.VERIFIER_CONFIG, logger=logger, tool_executor=tool_executor)
    return Verifier(llm=llm, prompt_file_path=prompt_file_path, logger=logger)

