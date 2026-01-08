import json
from agents.shared_context import SharedContext
from typing import Optional
from agents.utils import extract_substring, load_prompt_from_file
from llms.utils import LLMClient
from config.agent_config import AlphaSolveConfig
from .shared_context import *

from pocketflow import Node


CONJECTURE_BEGIN = '<conjecture>'
CONJECTURE_END = '</conjecture>'
FINAL_CONJECTURE_BEGIN = '<final_conjecture>'
FINAL_CONJECTURE_END = '</final_conjecture>'
PROOF_BEGIN = '<proof>'
PROOF_END = '</proof>'
DEPENDENCY_BEGIN = '<dependency>'
DEPENDENCY_END = '</dependency>'


class Solver(Node):
    
    def __init__(self, llm, problem, prompt_file_path, logger):
        super(Solver, self).__init__()
        self.problem = problem
        self.prompt_file_path = prompt_file_path
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def prep(self, shared): ## 按照 pocket-flow 的定义, 这一步是从 shard(一个dict) 里面拿出所有依赖
        # 在prep函数中读取shared的内容
        if self._quota_is_exhausted(shared):
            return AlphaSolveConfig.SOLVER_EXAUSTED, None

        prompt = self.__build_solver_prompt(
            prompt_template=self.prompt_template,
            problem=self.problem,
            verified_lemmas=[l for l in (shared["lemmas"] or []) if l.get("status") == "verified"],
            remaining_lemma_quota=AlphaSolveConfig.MAX_LEMMA_NUM - len(shared["lemmas"]),
            hint=shared["hint"],
        )

        messages_to_send = [{"role": "user", "content": prompt}]

        return AlphaSolveConfig.NORMAL, messages_to_send


    def exec(self, prep_res): ## 执行主要的逻辑
        ## 处理异常情况
        if not self._valid_prep_res(prep_res):
            self.logger.log_print('[solver] illegal prep_res in solver exec', module="solver", level="ERROR")
            return AlphaSolveConfig.EXIT_ON_ERROR, None, None, None

        code = prep_res[0]

        if code == AlphaSolveConfig.SOLVER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED, None, None, None

        messages = prep_res[1]
        _, _, updated_messages = self.llm.get_result(messages)

        lemma = self.__build_lemma(updated_messages)

        return AlphaSolveConfig.CONJECTURE_GENERATED, lemma, updated_messages


    def post(self, shared, prep_res, exec_res):  ## 更新一下iteration 变量
        # 在post函数中更新shared的内容

        # 处理异常情况
        if not self._valid_exec_res(exec_res):
            return AlphaSolveConfig.EXIT_ON_ERROR

        #处理solver步数耗尽
        if exec_res[0] == AlphaSolveConfig.EXIT_ON_EXAUSTED:
            self.logger.log_print('solver exhausted during post ...', module="solver", level="WARNING")
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        lemma = exec_res[1]
        # Validate lemma structure before updating shared.
        validate_lemma(lemma)
        shared["lemmas"].append(lemma)
        lemma_id = len(shared["lemmas"]) - 1
        shared["current_lemma_id"] = lemma_id

        self.logger.log_print(
            f"event=lemma_created step=post, lemma_id={lemma_id}, is_theorem={bool(lemma.get('is_theorem'))}, now has {len(shared['lemmas'])} lemmas",
            module="solver",
        )

        return AlphaSolveConfig.CONJECTURE_GENERATED


    def __build_solver_prompt(self, *, prompt_template, problem, verified_lemmas, remaining_lemma_quota, hint=None):

        tmp = prompt_template.replace('{problem_content}', problem)
        tmp = tmp.replace('{remaining_lemma_quota}', str(remaining_lemma_quota))

        # Only include VERIFIED lemmas in solver context.
        if verified_lemmas:
            lines = []
            lines.append("## Context and History Explorations")
            lines.append("")
            lines.append(
                "Here is a list of lemmas that we have collected for this problem or our history findings during exploration. "
                "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
            )
            lines.append("<memory>")
            for i, l in enumerate(verified_lemmas):
                lines.append(f" ** Lemma-{i} **")
                lines.append(f" {l.get('statement')}")
            lines.append("</memory>")
            tmp = tmp + "\n\n" + "\n".join(lines)

        if hint:
            tmp = tmp + "\n\n" + str(hint)

        self.logger.log_print("event=prompt_built step=prep", module="solver")
        return tmp


    def __build_lemma(self, messages)-> Optional[Lemma]:
        resp_from_llm = messages[-1]["content"]
        statement = extract_substring(
            resp_from_llm,
            CONJECTURE_BEGIN,
            CONJECTURE_END,
            logger=self.logger,
            module="solver",
        )
        final_statement = extract_substring(
            resp_from_llm,
            FINAL_CONJECTURE_BEGIN,
            FINAL_CONJECTURE_END,
            logger=self.logger,
            module="solver",
        )
        proof = extract_substring(
            resp_from_llm,
            PROOF_BEGIN,
            PROOF_END,
            logger=self.logger,
            module="solver",
        )
        dependencies = extract_substring(
            resp_from_llm,
            DEPENDENCY_BEGIN,
            DEPENDENCY_END,
            logger=self.logger,
            module="solver",
        )
        deps = []
        if dependencies:
            deps = json.loads(dependencies)  # expects JSON array of ints

        if statement and proof:
            return new_lemma(
                statement=statement,
                proof=proof,
                dependencies=deps,
                is_theorem=False,
                status="pending",
                history_messages=messages,
            )
        elif final_statement and proof:
            # Case: final conjecture + proof => theorem
            return new_lemma(
                statement=final_statement,
                proof=proof,
                dependencies=deps,
                is_theorem=True,
                status="pending",
                history_messages=messages,
            )
        return None
    
    def _quota_is_exhausted(self, shared):
        remaining_rounds = AlphaSolveConfig.MAX_LEMMA_NUM - len(shared["lemmas"])
        self.logger.log_print(
            f"solver_round_remaining={remaining_rounds}, {len(shared['lemmas'])} lemmas generated so far, max allowed={AlphaSolveConfig.MAX_LEMMA_NUM}",
            module="solver",
            )
        return remaining_rounds <= 0
    
    def _valid_prep_res(self, prep_res):
        if not prep_res or len(prep_res) < 2:
            self.logger.log_print('illegal prep_res with length: ', len(prep_res) if prep_res else 0, level="ERROR")
            return False
        return True
    
    def _valid_exec_res(self, exec_res):
        if not exec_res or len(exec_res) == 0:
            self.logger.log_print('illegal exec_res with length: ', len(exec_res) if exec_res else 0, level="ERROR")
            return False
        return True

def create_solver_agent(problem, prompt_file_path,logger):
    
    llm = LLMClient(module='solver', config=AlphaSolveConfig.SOLVER_CONFIG, logger=logger)
    return Solver(llm, problem, prompt_file_path, logger=logger)
