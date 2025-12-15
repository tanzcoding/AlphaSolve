import time, json, random
import agents.conjecture_graph

from agents.utils import load_prompt_from_file

from config.agent_config import AlphaSolveConfig, VERIFIER_CONFIG
from llms.utils import LLMClient

from pocketflow import Node

VERIFY_RESULT_VALID='boxed{valid}'
VERIFY_RESULT_INVALID='boxed{invalid}'


class Verifier(Node):

    def __init__(self, llm, problem, prompt_file_path):
        super(Verifier, self).__init__()
        self.problem = problem
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm 

    def prep(self, shared): 

        print('[verifier]in verifier ..., begin to build context ...')

        iteration = shared[AlphaSolveConfig.VERIFY_AND_REFINE_ROUND]
        
        if iteration == 0:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, None, None, None

        current_conj = shared[AlphaSolveConfig.CURRENT_CONJECTURE] 
        shared_context = shared[AlphaSolveConfig.SHARED_CONTEXT]

        reasoning_path = shared_context.build_context_for_conjecture(current_conj)        

        print('[verifier] in verifier ..., building context done ...')
        
        return AlphaSolveConfig.NORMAL, current_conj, reasoning_path, shared_context


    def exec(self, prep_res):

        if not prep_res or len(prep_res) == 0 or len(prep_res) < 4:
            return AlphaSolveConfig.EXIT_ON_ERROR, None, None, None

        code = prep_res[0]

        if code == AlphaSolveConfig.VERIFIER_EXAUSTED:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, None, None, None, None


        current_conj, reasoning_path, shared_context = prep_res[1], prep_res[2], prep_res[3]

        ## test time compute, 我们先直接撸 VERIFIER_SCALING_FACTOR 次, 任何一次错我们都认为错, 随机选择一个判错的 review 和 cot —— 这里和AIM不一样
        verifier_res = None

        result = [ ]

        for i in range(AlphaSolveConfig.VERIFIER_SCALING_FACTOR):
            is_valid, review, cot = self.__verify(current_conj, reasoning_path)
            print('[verifier] verifier test for iteration ', i, ' and result is: ', is_valid)

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
            return AlphaSolveConfig.NORMAL, is_valid, review, cot, current_conj, shared_context

        else:
            ## 说明全对了, 返回最后一次的结果
            return AlphaSolveConfig.NORMAL, verifier_res[0], verifier_res[1], verifier_res[2], current_conj, shared_context


    def post(self, shared, prep_res, exec_res): 
    
        ## post 做两件事情: (1) 返回决策(退出: 如果生成了 theorem, 改进: 走到refiner, 正确: 走到 solver ); (2) 把结果 submit 到 shared_context 里头

        if not exec_res or len(exec_res) < 6: ## 退出
            print('[verifier] illegal input in verifier exec')
            return AlphaSolveConfig.EXIT_ON_ERROR

        code, valid_conj, answer, cot, current_conj, shared_context = exec_res[0], exec_res[1], exec_res[2], exec_res[3], exec_res[4], exec_res[5]
 
        if code != AlphaSolveConfig.NORMAL:
            return AlphaSolveConfig.EXIT_ON_ERROR

        
        ## 更新 context 

        if valid_conj:  ## 此时说明验证通过了, 生成了正确的conj
            shared_context.submit(current_conj)
            if self.____judge_is_problem_solved(current_conf): ## 说明问题已经解决了
                return AlphaSolveConfig.DONE
            else: ## 说明引理正确但是还没有解决问题, 此时返回 solver
                return AlphaSolveConfig.CONJECTURE_VERIFIED
        else:
            current_conj.reveiw = answer
            return AlphaSolveConfig.CONJECTURE_UNVERIFIED

    def __verify(self, current_conj, reasoning_path):

        prompt = self.__build_verifier_prompt(current_conj, reasoning_path)

        b = time.time()
        messages_to_send = [
            {"role": "user", "content": prompt}
        ]
        answer, cot = self.llm.get_result(messages_to_send)

        print(f'[verifier] using: {time.time() - b:.1f}s, answer length: {len(answer)}, cot length: {len(cot)}')

        if VERIFY_RESULT_VALID in answer:
            return True, answer, cot
        else:
            return False, answer, cot


    def __judge_is_problem_solved(self, problem, conj):
        return conj.is_theorem


    def __build_verifier_prompt(self, conj, reasoning_path):
        ## 把所有东西拼到 prompt 里

        tmp = self.prompt_template.replace('{conjecture_content}', conj.conjecture).replace('{proof_content}', conj.proof)

        if reasoning_path:
            tmp = tmp + '\n' + reasoning_path
        
        return tmp



def create_verifier_agent(problem, prompt_file_path):

    llm = LLMClient(VERIFIER_CONFIG)
    return Verifier(llm, problem, prompt_file_path) 
