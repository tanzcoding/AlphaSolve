import time
import json
from agents.shared_context import SharedContext

from agents.utils import build_conjecture_helper
from agents.utils import load_prompt_from_file
from llms.utils import LLMClient
from config.agent_config import AlphaSolveConfig

from pocketflow import Node


CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'
DEPENDENCY_BEGIN = '\\begin{dependency}'
DEPENDENCY_END = '\\end{dependency}'

FINAL_BEGIN = '\\begin{final_proof}'
FINAL_END = '\\end{final_proof}'


class Solver(Node):
    
    def __init__(self, llm, problem, prompt_file_path, logger):
        super(Solver, self).__init__()
        self.problem = problem
        self.prompt_file_path = prompt_file_path
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.logger = logger

    def prep(self, shared): ## 按照 pocket-flow 的定义, 这一步是从 shard(一个dict) 里面拿出所有依赖
        # READ ONLY from shared here.
        iteration = shared["solver_round_remaining"]
        if iteration == 0:
            self.logger.log_print(
                "event=quota_exhausted step=prep remaining=0",
                module="solver",
                level="WARNING",
            )
            return AlphaSolveConfig.SOLVER_EXAUSTED, None

        hint = shared["hint"]

        remaining_lemma_quota = iteration
        prompt = self.__build_solver_prompt(
            prompt_template=self.prompt_template,
            problem=self.problem,
            verified_lemmas=[l for l in (shared["lemmas"] or []) if l.get("status") == "verified"],
            remaining_lemma_quota=remaining_lemma_quota,
            hint=hint,
        )

        messages_to_send = [{"role": "user", "content": prompt}]

        return AlphaSolveConfig.NORMAL, messages_to_send


    def exec(self, prep_res): ## 执行主要的逻辑
        ## 处理异常情况
        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        if len(prep_res) < 2:
            self.logger.log_print('illegal prep_res with length: ', len(prep_res), module="solver", level="ERROR")
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        code = prep_res[0]

        if code == AlphaSolveConfig.SOLVER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED, None

        b = time.time()
        messages = prep_res[1]
        # Solver 可使用工具（已在配置中设置）
        answer, cot, updated_messages = self.llm.get_result(messages)

        self.logger.log_print(
            f"event=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer)} cot_len={len(cot)}",
            module="solver",
        )

        lemma = self.__build_lemma(self.problem, answer, cot)
        return AlphaSolveConfig.CONJECTURE_GENERATED, lemma, updated_messages


    def post(self, shared, prep_res, exec_res):  ## 更新一下iteration 变量
        # WRITE ONLY to shared here.
        if not exec_res or len(exec_res) == 0:
            self.logger.log_print('[solver] illegal exec_res in solver post', module="solver", level="ERROR")
            return AlphaSolveConfig.EXIT_ON_ERROR

        #处理solver步数耗尽
        if exec_res[0] == AlphaSolveConfig.EXIT_ON_EXAUSTED:
            self.logger.log_print('[solver] solver exhausted during post ...', module="solver", level="WARNING")
            return AlphaSolveConfig.EXIT_ON_EXAUSTED


        # 不知道为什么没有生成引理, 直接重新开始
        if not exec_res[1]:
            self.logger.log_print('[solver] no conjecture generated ...', module="solver", level="ERROR")
            return AlphaSolveConfig.EXIT_ON_ERROR

        # decrement solver rounds
        shared["solver_round_remaining"] = shared["solver_round_remaining"] - 1

        # reset verify/refine rounds
        shared["verify_refine_round_remaining"] = AlphaSolveConfig.VERIFY_AND_REFINE_ROUND_NUM

        lemma = exec_res[1]
        # Validate lemma structure before mutating shared.
        SharedContext.validate_lemma(lemma)
        shared["lemmas"].append(lemma)
        lemma_id = len(shared["lemmas"]) - 1
        shared["current_lemma_id"] = lemma_id

        shared["messages_for_refiner"] = exec_res[2] if len(exec_res) > 2 else None

        self.logger.log_print(
            f"event=lemma_created step=post lemma_id={lemma_id} is_theorem={bool(lemma.get('is_theorem'))} solver_round_remaining={shared['solver_round_remaining']}",
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
                "Here is a list of context that we have collected for this problem or our history findings during exploration. "
                "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
            )
            lines.append("")
            for i, l in enumerate(verified_lemmas):
                lines.append(f" ** Conjecture-{i} **")
                lines.append(f" {l.get('statement')}")
            tmp = tmp + "\n\n" + "\n".join(lines)

        if hint:
            tmp = tmp + "\n\n" + str(hint)

        self.logger.log_print("event=prompt_built step=prep", module="solver")
        return tmp


    def __build_lemma(self, problem, resp_from_llm, cot=None):

        conj = build_conjecture_helper(
            resp_from_llm,
            CONJECTURE_BEGIN,
            CONJECTURE_END,
            logger=self.logger,
            module="solver",
        )
        proof = build_conjecture_helper(
            resp_from_llm,
            PROOF_BEGIN,
            PROOF_END,
            logger=self.logger,
            module="solver",
        )

        dependencies = build_conjecture_helper(
            resp_from_llm,
            DEPENDENCY_BEGIN,
            DEPENDENCY_END,
            logger=self.logger,
            module="solver",
        )
        deps = []
        if dependencies:
            deps = json.loads(dependencies)  # expects JSON array of ints

        is_theorem = False
        final_proof = build_conjecture_helper(
            resp_from_llm,
            FINAL_BEGIN,
            FINAL_END,
            logger=self.logger,
            module="solver",
        )

        # Case: final proof => theorem
        if final_proof:
            conj = problem
            proof = final_proof
            is_theorem = True
        else:
            if not conj or not proof:
                return None

        return SharedContext.new_lemma(
            statement=conj,
            proof=proof,
            dependencies=deps,
            is_theorem=is_theorem,
            status="pending",
            cot=cot,
        )
       

def create_solver_agent(problem, prompt_file_path,logger):
    
    llm = LLMClient(AlphaSolveConfig.SOLVER_CONFIG, logger=logger)
    return Solver(llm, problem, prompt_file_path, logger=logger)
