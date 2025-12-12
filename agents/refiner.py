import time
import json
import agents.conjecture_graph

from agents.utils import build_conjuecture_helper
from agents.utils import load_prompt_from_file
from llms.kimi import KimiClient
from llms.deepseek import DeepSeekClient
from config.agent_config import AlphaSolveConfig

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'

class Refiner(Node):

    def __init__(self, llm, model, prompt_file_path): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Refiner, self).__init__()
        self.model = model
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm 

    def prep(self,shared): 

        print('[refiner] building refiner context...')
 
        conj = shared[AlphaSolveConfig.CURRENT_CONJECTURE]
        shared_context = shared[AlphaSolveConfig.SHARED_CONTEXT]
        print('[refiner] in refiner ..., building context done ...')
 
        return conj, shared_context.build_context_for_conjecture(conj), shared_context

    def exec(self, prep_res): 
   
        if not prep_res or len(prep_res) < 3:
            return AlphaSolveConfig.EXIT_ON_ERROR

        conj, reasoning_path, shared_context  = prep_res[0], prep_res[1], prep_res[2]
         
        if not conj.conjecture or not conj.proof:
            return AlphaSolveConfig.EXIT_ON_ERROR

        valid, rationale = self.__refine(conj, reasoning_path)

        return valid, rationale, conj, shared_context

    def post(self, shared, prep_res, exec_res): 

        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR
        if not exec_res: ## 应该是出错了, 重新 refine 一次吧, 没啥意义, 容错用的
            return AlphaSolveConfig.EXIT_ON_ERROR

        is_conjecture_valid, next_conjecture = exec_res[0], exec_res[1]
        
        if is_conjecture_valid:
            if next_conjecture:
                return AlphaSolveConfig.REFINE_SUCCESS
            else:
                return  AlphaSolveConfig.EXIT_ON_ERROR
        else: ## 代表此时 refiner 觉得
            return AlphaSolveConfig.CONJECTURE_WRONG
 

    def __refine(self, conj, reasoning_path): 

        prompt = self.__build_refiner_prompt(conj, reasoning_path)

        b = time.time()

        resp = self.llm.get_result('', prompt)

        answer, cot = resp[0], resp[1]

        print('[refiner] using:', time.time() - b, len(answer), len(cot))

        conj2, proof = self.__extract_from_model(answer)

        valid =  INVALID_TAG in answer 

        if conj and proof:
            return valid, conj.create_sub(conj2, proof)            
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



def create_refiner_agent(model, prompt_file_path):
 
    ds = DeepSeekClient()
    return Refiner(ds, model, prompt_file_path) 
