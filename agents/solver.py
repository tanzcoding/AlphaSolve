import json
from agents.shared_context import SharedContext, save_snapshot
from typing import Optional
from utils.utils import extract_substring, load_prompt_from_file
from llms.utils import LLMClient
from config.agent_config import AlphaSolveConfig
from .shared_context import *
from utils.logger import Logger

from pocketflow import Node


CONJECTURE_BEGIN = r'\begin{conjecture}'
CONJECTURE_END = r'\end{conjecture}'
PROOF_BEGIN = r'\begin{proof}'
PROOF_END = r'\end{proof}'
DEPENDENCY_BEGIN = r'\begin{dependency}'
DEPENDENCY_END = r'\end{dependency}'


class Solver(Node):
    
    def __init__(self, llm: LLMClient, prompt_file_path, logger: Logger):
        super(Solver, self).__init__()
        self.prompt_file_path = prompt_file_path
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def prep(self, shared): ## 按照 pocket-flow 的定义, 这一步是从 shard(一个dict) 里面拿出所有依赖
        # 在prep函数中读取shared的内容
        self.logger.log_print('entering solver...', module='solver')

        if self._quota_is_exhausted(shared):
            return AlphaSolveConfig.SOLVER_EXAUSTED, None

        prompt = self.__build_solver_prompt(
            prompt_template=self.prompt_template,
            problem=shared["problem"],
            verified_lemmas=[l for l in (shared["lemmas"] or []) if l.get("status") == "verified"],
            remaining_lemma_quota=AlphaSolveConfig.MAX_LEMMA_NUM - len(shared["lemmas"]),
            hint=shared["hint"],
        )

        messages_to_send = [{"role": "system", "content": "You are an expert mathematician. You will be given a problem and a list of Lemmas (if any) we have established. Try to propose a new conjecture that can help solve the problem at hand. If your conjecture is verified by the user, it will be added to our list of Lemmas. "},
                            {"role": "user", "content": prompt}]

        return AlphaSolveConfig.NORMAL, messages_to_send,shared


    def exec(self, prep_res): ## 执行主要的逻辑
        ## 处理异常情况
        if not self._valid_prep_res(prep_res):
            self.logger.log_print('[solver] illegal prep_res in solver exec', module="solver", level="ERROR")
            return AlphaSolveConfig.EXIT_ON_ERROR, None, None, None

        code = prep_res[0]

        if code == AlphaSolveConfig.SOLVER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED, None, None, None

        messages = prep_res[1]
        shared = prep_res[2]
        _, _, updated_messages = self.llm.get_result(messages,shared=shared)

        lemma = self.__build_lemma(updated_messages)

        return AlphaSolveConfig.CONJECTURE_GENERATED, lemma, updated_messages


    def post(self, shared, prep_res, exec_res):  ## 更新一下iteration 变量
        # 在post函数中更新shared的内容

        # 处理异常情况
        if not self._valid_exec_res(exec_res):
            self.logger.log_print('exiting solver...', module='solver')
            save_snapshot(shared, "solver", AlphaSolveConfig.EXIT_ON_ERROR)
            return AlphaSolveConfig.EXIT_ON_ERROR

        #处理solver步数耗尽
        if exec_res[0] == AlphaSolveConfig.EXIT_ON_EXAUSTED:
            self.logger.log_print('solver exhausted during post ...', module="solver", level="WARNING")
            self.logger.log_print('exiting solver...', module='solver')
            save_snapshot(shared, "solver", AlphaSolveConfig.EXIT_ON_EXAUSTED)
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        lemma = exec_res[1]
        # Validate lemma structure before updating shared.
        validate_lemma(lemma)

        if not lemma['is_theorem']:
            self.logger.log_print(
                f"event=check_theorem step=post",
                module="solver",
            )
            check_message = f"Check if the following statement **fully addresses the problem** (do NOT check if the statement is mathematically correct - only check if it answers the problem). Output ONLY 'Yes' or 'No' without any explanation.\n\nProblem: {shared['problem']}\n\nStatement: {lemma['statement']}"
            response,_,_ = self.llm.get_result(messages=[{"role": "user", "content": check_message}],tools=None,shared=shared)
            answer = response.strip().lower()
            if answer == 'yes':
                lemma['is_theorem'] = True
                self.logger.log_print(
                    f"event=lemma_marked_as_theorem step=post",
                    module="solver",
                )

        shared["lemmas"].append(lemma)
        lemma_id = len(shared["lemmas"]) - 1
        shared["current_lemma_id"] = lemma_id

        self.logger.log_print(
            f"event=lemma_created step=post, lemma_id={lemma_id}, is_theorem={bool(lemma.get('is_theorem'))}, now has {len(shared['lemmas'])} lemmas",
            module="solver",
        )

        self.logger.log_print('exiting solver...', module='solver')
        
        # Save snapshot after updating shared
        save_snapshot(shared, "solver", AlphaSolveConfig.CONJECTURE_GENERATED)
        
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
                "Here is a list of lemma that we have collected for this problem or our history findings during exploration. "
                "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
                "You can also use the 'read_lemma' tool to read the proof of a lemma. By doing so, you can learn from the previous proof(s) and extend them to help you construct new conjectures and proofs."
            )
            lines.append("")
            for i, l in enumerate(verified_lemmas):
                lines.append(f" ** Lemma-{i} **")
                lines.append(f" {l.get('statement')}")
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
                verify_round=0,
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
        if not prep_res or len(prep_res) < 3:
            self.logger.log_print('illegal prep_res with length: ', len(prep_res) if prep_res else 0, level="ERROR")
            return False
        return True
    
    def _valid_exec_res(self, exec_res):
        if not exec_res or len(exec_res) == 0:
            self.logger.log_print('illegal exec_res with length: ', len(exec_res) if exec_res else 0, level="ERROR")
            return False
        return True

def create_solver_agent(prompt_file_path,logger):
    
    llm = LLMClient(module='solver', config=AlphaSolveConfig.SOLVER_CONFIG, logger=logger)
    return Solver(llm, prompt_file_path, logger=logger)
