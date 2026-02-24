from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, List

from agents.shared_context import Lemma, new_lemma
from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient, ParallelLLMClient
from utils.logger import Logger
from utils.utils import extract_substring, load_prompt_from_file


CONJECTURE_BEGIN = r'\begin{conjecture}'
CONJECTURE_END = r'\end{conjecture}'
PROOF_BEGIN = r'\begin{proof}'
PROOF_END = r'\end{proof}'
DEPENDENCY_BEGIN = r'\begin{dependency}'
DEPENDENCY_END = r'\end{dependency}'


@dataclass
class GenerateInput:
    problem: str
    hint: Optional[str]
    verified_context: List[Lemma]
    remaining_lemma_quota: int
    iteration_round: int = 0
    mode: Optional[str] = None


@dataclass
class GenerateOutput:
    lemma: Optional[Lemma]
    done: bool


class Generator:
    def __init__(self, llm: LLMClient, prompt_file_path: str, logger: Logger):
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def generate(self, input: GenerateInput) -> GenerateOutput:
        if input.remaining_lemma_quota <= 0:
            return GenerateOutput(lemma=None, done=False)

        prompt = self._build_generator_prompt(
            prompt_template=self.prompt_template,
            problem=input.problem,
            lemmas=input.verified_context,
            remaining_lemma_quota=input.remaining_lemma_quota,
            iteration_round=input.iteration_round,
            mode=input.mode,
            hint=input.hint,
        )

        messages_to_send = [
            {"role": "system", "content": "You are an expert mathematician. You will be given a problem and a list of Lemmas (if any) we have established. Try to propose a new conjecture that can help solve the problem at hand. If your conjecture is verified by the user, it will be added to our list of Lemmas. "},
            {"role": "user", "content": prompt},
        ]

        shared = {
            "problem": input.problem,
            "hint": input.hint,
            "lemmas": input.verified_context,
            "current_lemma_id": None,
            "result_summary": None,
        }

        updated_messages = messages_to_send
        lemma = None
        for _ in range(AlphaSolveConfig.GENERATOR_MAX_RETRY):
            _, _, updated_messages = self.llm.get_result(messages_to_send, tools=AlphaSolveConfig.GENERATOR_CONFIG['tools'], shared=shared)
            lemma = self._build_lemma(updated_messages)
            if lemma:
                break

        if not lemma:
            return GenerateOutput(lemma=None, done=False)

        is_theorem = self._check_is_theorem(problem=input.problem, statement=lemma["statement"], shared=shared)
        lemma["is_theorem"] = is_theorem
        return GenerateOutput(lemma=lemma, done=is_theorem)

    def _build_generator_prompt(self, *, prompt_template, problem, lemmas, remaining_lemma_quota, iteration_round, mode, hint=None):
        tmp = prompt_template.replace('{problem_content}', problem)
        tmp = tmp.replace('{remaining_lemma_quota}', str(remaining_lemma_quota))

        if lemmas:
            lines = [
                "## Context and History Explorations",
                "",
                "Here is a list of lemma that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.You can also use the 'read_lemma' tool to read the proof of a lemma. By doing so, you can learn from the previous proof(s) and extend them to help you construct new conjectures and proofs.",
                "",
            ]
            for i, lemma in enumerate(lemmas):
                lines.append(f" ** Lemma-{i} **")
                lines.append(f" {lemma.get('statement')}")
            tmp = tmp + "\n\n" + "\n".join(lines)

        if hint:
            tmp = tmp + "\n\n" + "## Hint and Suggestions" + "\n\n" + str(hint)

        return tmp

    def _build_lemma(self, messages) -> Optional[Lemma]:
        resp_from_llm = messages[-1]["content"]
        statement = extract_substring(resp_from_llm, CONJECTURE_BEGIN, CONJECTURE_END, logger=self.logger, module="generator")
        proof = extract_substring(resp_from_llm, PROOF_BEGIN, PROOF_END, logger=self.logger, module="generator")
        dependencies = extract_substring(resp_from_llm, DEPENDENCY_BEGIN, DEPENDENCY_END, logger=self.logger, module="generator")

        deps = []
        if dependencies:
            try:
                deps = json.loads(dependencies)
            except Exception:
                deps = []

        if statement and proof:
            return new_lemma(
                statement=statement,
                proof=proof,
                dependencies=deps,
                is_theorem=False,
                status="pending",
                history_messages=messages,
                verify_round=0,
            )
        return None

    def _check_is_theorem(self, *, problem: str, statement: str, shared: dict) -> bool:
        is_theorem_num = 0
        for _ in range(AlphaSolveConfig.CHECK_IS_THEOREM_TIMES):
            check_message = (
                "Check if the following statement fully addresses the problem "
                "(do NOT check if the statement is mathematically correct - only check if it fully resolves the problem). "
                "Output ONLY 'Yes' or 'No' without any explanation.\n\n"
                f"Problem: {problem}\n\nStatement: {statement}"
            )
            response, _, _ = self.llm.get_result(messages=[{"role": "user", "content": check_message}], tools=[], shared=shared)
            if response.strip().lower() == 'yes':
                is_theorem_num += 1
            else:
                return False
        return is_theorem_num >= AlphaSolveConfig.CHECK_IS_THEOREM_TIMES


def create_generator_component(prompt_file_path: str, logger: Logger, tool_executor=None) -> Generator:
    if not tool_executor:
        llm = LLMClient(module='generator', config=AlphaSolveConfig.GENERATOR_CONFIG, logger=logger)
    else:
        llm = ParallelLLMClient(module='generator', config=AlphaSolveConfig.GENERATOR_CONFIG, logger=logger, tool_executor=tool_executor)
    return Generator(llm=llm, prompt_file_path=prompt_file_path, logger=logger)

