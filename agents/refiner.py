import time
import json
import agents.conjecture_graph

from agents.utils import build_conjuecture_helper
from agents.utils import load_prompt_from_file

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import log_print

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'

class Refiner(Node):

    def __init__(self, llm, prompt_file_path, print_to_console): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Refiner, self).__init__()
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm 
        self.print_to_console = print_to_console

    def prep(self,shared): 

        log_print('[refiner] building refiner context...', print_to_console=self.print_to_console)

        iteration = shared[AlphaSolveConfig.VERIFY_AND_REFINE_ROUND]

        if iteration == 0:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, None, None, None, None
 
        conj = shared[AlphaSolveConfig.CURRENT_CONJECTURE]
        shared_context = shared[AlphaSolveConfig.SHARED_CONTEXT]

        log_print('[refiner] in refiner ..., building context done ...', print_to_console=self.print_to_console)
 
        return AlphaSolveConfig.NORMAL, conj, shared_context.build_context_for_conjecture(conj), shared_context, self.print_to_console

    def exec(self, prep_res): 
   
        if not prep_res or len(prep_res) < 5:
            return AlphaSolveConfig.EXIT_ON_ERROR

        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, True, None, None, None

        conj, reasoning_path, shared_context  = prep_res[1], prep_res[2], prep_res[3]
         
        if not conj.conjecture or not conj.proof:
            return AlphaSolveConfig.EXIT_ON_ERROR

        valid, new_conj = self.__refine(conj, reasoning_path, self.print_to_console, shared_context)

        return AlphaSolveConfig.NORMAL, valid, new_conj, conj, shared_context

    def post(self, shared, prep_res, exec_res): 

        if not prep_res:
            log_print('[refiner] illegal prep_res in refiner post', print_to_console=self.print_to_console)
            return AlphaSolveConfig.EXIT_ON_ERROR
        if not exec_res: ## 应该是出错了, 重新 refine 一次吧, 没啥意义, 容错用的
            log_print('[refiner] illegal exec_res in refiner post', print_to_console=self.print_to_console)
            return AlphaSolveConfig.EXIT_ON_ERROR

        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        is_conjecture_valid, next_conjecture = exec_res[1], exec_res[2]
        
        ## 更新 iteration 参数
        iteration = shared[AlphaSolveConfig.VERIFY_AND_REFINE_ROUND]
        iteration -= 1
        shared[AlphaSolveConfig.VERIFY_AND_REFINE_ROUND] = iteration

        if is_conjecture_valid:
            if next_conjecture:
                log_print('[refiner] refine success, new conjecture generated ...', print_to_console=self.print_to_console)
                ## 后续都基于这条 conj 工作
                shared[AlphaSolveConfig.CURRENT_CONJECTURE] = next_conjecture

                return AlphaSolveConfig.REFINE_SUCCESS
            else:
                log_print('[refiner] refine failed, no new conjecture generated ...', print_to_console=self.print_to_console)
                return  AlphaSolveConfig.EXIT_ON_ERROR

        else: ## 代表此时 refiner 觉得猜想不对，要返回solver
            log_print('[refiner] conjecture wrong, need to go back to solver ...', print_to_console=self.print_to_console)
            return AlphaSolveConfig.CONJECTURE_WRONG
 

    def __refine(self, conj, reasoning_path, print_to_console, shared_context): 

        prompt = self.__build_refiner_prompt(conj, reasoning_path)

        b = time.time()
        messages_to_send = [
            {"role": "user", "content": prompt}
        ]
        answer, cot = self.llm.get_result(messages_to_send, print_to_console=print_to_console)

        log_print(f'[refiner] using: {time.time() - b:.1f}s, answer length: {len(answer)}, cot length: {len(cot)}', print_to_console=self.print_to_console)

        conj2, proof = self.__extract_from_model(answer)

        valid =  INVALID_TAG not in answer 

        if conj and proof:
            return valid, shared_context.add_to_conjecture_graph_by_parent(conj, conj2, proof, cot)           
        else:
            return valid, None


    def __extract_from_model(self, model_output):
        
        conj = build_conjuecture_helper(model_output, CONJECTURE_BEGIN, CONJECTURE_END)
        proof = build_conjuecture_helper(model_output, PROOF_BEGIN, PROOF_END)        

        return conj, proof

    def __build_refiner_prompt(self, conjecture, reasoning_path): ## 把所有东西拼到 prompt 里

        if not conjecture.conjecture or not conjecture.proof: ## 容错
            return None

        tmp = self.prompt_template.replace('{conjecture_content}', conjecture.conjecture).replace('{proof_content}', conjecture.proof)

        if conjecture.review:
            tmp = tmp.replace('{review_content}', conjecture.review)

        if reasoning_path:
            tmp = tmp + '\n' + reasoning_path

        return tmp


def create_refiner_agent(prompt_file_path, print_to_console):
 
    llm = LLMClient(AlphaSolveConfig.REFINER_CONFIG, print_to_console=print_to_console)
    return Refiner(llm, prompt_file_path, print_to_console)
