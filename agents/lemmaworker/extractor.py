from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List

from agents.shared_context import Lemma
from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient, ParallelLLMClient
from utils.logger import Logger
from utils.utils import extract_substring, load_prompt_from_file


CONJECTURE_BEGIN = r'\begin{conjecture}'
CONJECTURE_END = r'\end{conjecture}'
PROOF_BEGIN = r'\begin{proof}'
PROOF_END = r'\end{proof}'


@dataclass
class ExtractInput:
    problem: str
    verified_context: List[Lemma]
    candidate_lemma: Lemma


@dataclass
class ExtractOutput:
    new_statement: Optional[str]
    new_proof: Optional[str]
    rejected: bool


class Extractor:
    def __init__(self, llm: LLMClient, prompt_file_path: str, logger: Logger):
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def extract(self, input: ExtractInput) -> ExtractOutput:
        ctx_text = self._render_context(input.verified_context)
        prompt = self._build_extractor_prompt(input.candidate_lemma, ctx_text)

        messages_to_send = [
            {"role": "user", "content": prompt}
        ]
        shared = {
            "problem": input.problem,
            "hint": None,
            "lemmas": input.verified_context,
            "current_lemma_id": None,
            "current_lemma": input.candidate_lemma,
            "result_summary": None,
        }

        response = ""
        for _ in range(AlphaSolveConfig.REVISER_MAX_RETRY):
            response, _, _ = self.llm.get_result(messages=messages_to_send, tools=AlphaSolveConfig.REVISER_CONFIG['tools'], shared=shared)
            if self._validate_response(response):
                break

        new_statement = extract_substring(response, CONJECTURE_BEGIN, CONJECTURE_END, logger=self.logger, module="extractor")
        new_proof = extract_substring(response, PROOF_BEGIN, PROOF_END, logger=self.logger, module="extractor")
        rejected = not bool((new_statement and len(new_statement) > 5) or (new_proof and len(new_proof) > 5))
        return ExtractOutput(new_statement=new_statement, new_proof=new_proof, rejected=rejected)

    def _build_extractor_prompt(self, lemma: Lemma, reasoning_ctx: str) -> Optional[str]:
        if not lemma.get("statement") or not lemma.get("proof"):
            return None
        tmp = self.prompt_template.replace('{conjecture_content}', lemma.get("statement", "")).replace('{proof_content}', lemma.get("proof", ""))
        if lemma.get("review"):
            tmp = tmp.replace('{review_content}', lemma.get("review"))
        if reasoning_ctx:
            tmp = tmp + '\n' + reasoning_ctx
        return tmp

    def _validate_response(self, response: str) -> bool:
        return self._has_unique(response, CONJECTURE_BEGIN, CONJECTURE_END) and self._has_unique(response, PROOF_BEGIN, PROOF_END)

    def _has_unique(self, response: str, begin: str, end: str) -> bool:
        begin_count = response.count(begin)
        end_count = response.count(end)
        if not (begin_count == 1 and end_count == 1):
            return False
        begin_index = response.find(begin)
        end_index = response.find(end)
        return begin_index < end_index

    def _render_context(self, lemmas: List[Lemma]) -> str:
        if not lemmas:
            return ""
        lines = [
            "## Context and History Explorations",
            "",
            "Here is a list of lemma that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.",
            "",
        ]
        for i, lemma in enumerate(lemmas):
            lines.append(f" ** Lemma-{i} **")
            lines.append(f" {lemma.get('statement')}")
        return "\n".join(lines)


def create_extractor_component(prompt_file_path: str, logger: Logger, tool_executor=None) -> Extractor:
    if not tool_executor:
        llm = LLMClient(module='extractor', config=AlphaSolveConfig.EXTRACTOR_CONFIG, logger=logger)
    else:
        llm = ParallelLLMClient(module='extractor', config=AlphaSolveConfig.EXTRACTOR_CONFIG, logger=logger, tool_executor=tool_executor)
    return Extractor(llm=llm, prompt_file_path=prompt_file_path, logger=logger)
