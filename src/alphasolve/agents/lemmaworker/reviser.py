from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List

from alphasolve.agents.shared_context import Lemma
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.llms.utils import LLMClient, ParallelLLMClient
from alphasolve.utils.logger import Logger
from alphasolve.utils.utils import extract_substring, load_prompt_from_file


CONJECTURE_BEGIN = r'\begin{conjecture}'
CONJECTURE_END = r'\end{conjecture}'
PROOF_BEGIN = r'\begin{proof}'
PROOF_END = r'\end{proof}'


@dataclass
class ReviseInput:
    problem: str
    verified_context: List[Lemma]
    candidate_lemma: Lemma


@dataclass
class ReviseOutput:
    new_statement: Optional[str]
    new_proof: Optional[str]
    rejected: bool


class Reviser:
    def __init__(self, llm: LLMClient, prompt_file_path: str, logger: Logger):
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def revise(self, input: ReviseInput) -> ReviseOutput:
        ctx_text = self._render_context(input.verified_context)
        prompt = self._build_reviser_prompt(input.candidate_lemma, ctx_text)

        messages_to_send = [
            {"role": "system", "content": """You are an expert mathematical reviser. Your task is to fix errors in conjectures and proofs or even write a new conjecture and proof based on review comments.

IMPORTANT: You SHOULD do the high level planning, and use the available subagent tools to do concrete works during your revision:

1. **call_proof_subagent**: Use this whenever you need to prove or verify a bounded mathematical proposition/claim/statement when revising the proof. Delegate small, self-contained proof tasks to this subagent.

2. **call_compute_subagent**: Use this EARLY and OFTEN for any calculation, symbolic simplification, equation solving, numeric testing, counterexample finding, or edge-case checking when revising. If you catch yourself "working it out manually, STOP and delegate to this subagent instead.

How to use subagents:
- Think about what methods could help you revise the proof effectively, but DO NOT get bogged down in the details yourself
- Decompose your revision into small, concrete subtasks
- Call the appropriate subagent for each subtask
- Use the subagent results to inform your revision
- You may call multiple subagents in sequence as needed"""},
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

        new_statement = extract_substring(response, CONJECTURE_BEGIN, CONJECTURE_END, logger=self.logger, module="reviser")
        new_proof = extract_substring(response, PROOF_BEGIN, PROOF_END, logger=self.logger, module="reviser")
        rejected = not bool((new_statement and len(new_statement) > 5) or (new_proof and len(new_proof) > 5))
        return ReviseOutput(new_statement=new_statement, new_proof=new_proof, rejected=rejected)

    def _build_reviser_prompt(self, lemma: Lemma, reasoning_ctx: str) -> Optional[str]:
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


def create_reviser_component(prompt_file_path: str, logger: Logger, tool_executor=None) -> Reviser:
    if not tool_executor:
        llm = LLMClient(module='reviser', config=AlphaSolveConfig.REVISER_CONFIG, logger=logger)
    else:
        llm = ParallelLLMClient(module='reviser', config=AlphaSolveConfig.REVISER_CONFIG, logger=logger, tool_executor=tool_executor)
    return Reviser(llm=llm, prompt_file_path=prompt_file_path, logger=logger)
