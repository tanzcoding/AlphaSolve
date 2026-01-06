import time, json, random

from agents.utils import load_prompt_from_file

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node

VERIFY_RESULT_VALID='boxed{valid}'
VERIFY_RESULT_INVALID='boxed{invalid}'


class Verifier(Node):

    def __init__(self, llm, problem, prompt_file_path, logger):
        super(Verifier, self).__init__()
        self.problem = problem
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm
        self.logger = logger

    def prep(self, shared): 
        # READ ONLY from shared here.
        lemma_id = shared["current_lemma_id"]
        if lemma_id is None:
            self.logger.log_print(
                "event=no_current_lemma step=prep",
                module="verifier",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        lemmas = shared["lemmas"]
        lemma = lemmas[lemma_id]

        ctx_ids = shared.build_reasoning_path(lemma_id, verified_only=True)
        ctx_text = self.__render_context(ctx_ids, lemmas)
        self.logger.log_print(
            f"event=context_built step=prep lemma_id={lemma_id} ctx_size={len(ctx_ids)}",
            module="verifier",
        )

        return AlphaSolveConfig.NORMAL, lemma_id, lemma, ctx_text


    def exec(self, prep_res):
        if not prep_res or len(prep_res) < 4:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        code = prep_res[0]
        if code != AlphaSolveConfig.NORMAL:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        lemma_id, lemma, reasoning_ctx = prep_res[1], prep_res[2], prep_res[3]

        ## test time compute, 我们先直接撸 VERIFIER_SCALING_FACTOR 次, 任何一次错我们都认为错, 随机选择一个判错的 review 和 cot —— 这里和AIM不一样
        verifier_res = None

        result = [ ]

        for i in range(AlphaSolveConfig.VERIFIER_SCALING_FACTOR):
            is_valid, review, cot = self.__verify(lemma, reasoning_ctx)
            self.logger.log_print(
                f"event=verify_try step=exec lemma_id={lemma_id} try={i} valid={is_valid}",
                module="verifier",
            )

            if not is_valid: ## 发现错误就直接退出了, 其实也可以不退出, 看看多次能否发现不同的错误
                result.append((is_valid, review, cot))
                break
            else:
                verifier_res = is_valid, review, cot

        if len(result) > 0:  ## 说明有错误, 如果要多次 scaling, 可以采用某种 merge 策略
            index = 0
            if len(result) > 1:
                index = random.randint(0, len(result))

            is_valid, review, cot = result[index]
            return AlphaSolveConfig.NORMAL, lemma_id, is_valid, review, cot

        else:
            ## 说明全对了, 返回最后一次的结果
            return AlphaSolveConfig.NORMAL, lemma_id, verifier_res[0], verifier_res[1], verifier_res[2]


    def post(self, shared, prep_res, exec_res): 
    
        ## post 做两件事情: (1) 返回决策(退出: 如果生成了 theorem, 改进: 走到refiner, 正确: 走到 solver ); (2) 把结果 submit 到 shared_context 里头

        # WRITE ONLY to shared here.
        if not exec_res or len(exec_res) < 5:
            self.logger.log_print(
                "event=illegal_exec_res step=post",
                module="verifier",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        code, lemma_id, is_valid, review, cot = exec_res[0], exec_res[1], exec_res[2], exec_res[3], exec_res[4]
  
        if code != AlphaSolveConfig.NORMAL:
            self.logger.log_print(
                "event=exec_error step=post",
                module="verifier",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        
        lemmas = shared["lemmas"]
        lemma = lemmas[lemma_id]

        if is_valid:
            lemma["status"] = "verified"
            lemma["review"] = review
            lemma["cot"] = cot

            if lemma.get("is_theorem"):
                self.logger.log_print(
                    f"event=theorem_verified step=post lemma_id={lemma_id}",
                    module="verifier",
                )
                return AlphaSolveConfig.DONE

            self.logger.log_print(
                f"event=lemma_verified step=post lemma_id={lemma_id}",
                module="verifier",
            )
            return AlphaSolveConfig.CONJECTURE_VERIFIED

        lemma["status"] = "pending"
        lemma["review"] = review
        lemma["cot"] = cot
        self.logger.log_print(
            f"event=lemma_unverified step=post lemma_id={lemma_id}",
            module="verifier",
            level="WARNING",
        )
        return AlphaSolveConfig.CONJECTURE_UNVERIFIED

    def __verify(self, lemma, reasoning_ctx):

        prompt = self.__build_verifier_prompt(lemma, reasoning_ctx)

        b = time.time()
        messages_to_send = [
            {"role": "user", "content": prompt}
        ]

        # For audit/debug/HTML: record the exact messages sent to LLM.
        self.logger.log_print(
            "event=llm_messages step=exec\n" + json.dumps(messages_to_send, ensure_ascii=False, indent=2),
            module="verifier",
        )

        answer, cot = self.llm.get_result(messages_to_send)

        self.logger.log_print(
            f"event=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer)} cot_len={len(cot)}",
            module="verifier",
        )

        if VERIFY_RESULT_VALID in answer:
            return True, answer, cot
        else:
            return False, answer, cot

    def __build_verifier_prompt(self, lemma, reasoning_ctx):
        ## 把所有东西拼到 prompt 里

        tmp = self.prompt_template.replace('{conjecture_content}', lemma.get('statement', '')).replace('{proof_content}', lemma.get('proof', ''))

        if reasoning_ctx:
            tmp = tmp + '\n' + reasoning_ctx
        
        return tmp

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



def create_verifier_agent(problem, prompt_file_path, logger):

    llm = LLMClient(AlphaSolveConfig.VERIFIER_CONFIG, logger=logger)
    return Verifier(llm, problem, prompt_file_path, logger)
