import time
import json
import agents.conjecture_graph
from llms.volcano_ds import get_result
from agents.utils import build_conjuecture_helper

CONTEXT_PREFIX = '''
## Context and History Explorations

Here is a list of context that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.

{context_content}
'''

CONJECTURE_PREFIX = '''
 ** Conjecture-{index} **
 {content}
'''


## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'


class Refiner:

    def __init__(self, conjecture, model, prompt_file_path, shared_context): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        self.conjecture = conjecture
        self.model = model
        self.prompt_file_path = prompt_file_path
        self.prompt = self.__load_refiner_prompt()
 
    def refine(self): 
        prompt = self.__build_refiner_prompt()

        b = time.time()

        resp = get_result('', prompt)

        answer, cot = resp[0], resp[1]

        print('using:', time.time() - b, len(answer), len(cot))

        conj, proof = self.__extract_from_model(answer)

        valid =  INVALID_TAG in answer 


        if conj and proof:
            return valid, self.conjecture.create_sub(conj, proof)            
        else:
            return valid, None


    def __extract_from_model(self, model_output):
        
        conj = build_conjuecture_helper(model_output, CONJECTURE_BEGIN, CONJECTURE_END)
        proof = build_conjuecture_helper(model_output, PROOF_BEGIN, PROOF_END)        

        return conj, proof

    def __build_refiner_prompt(self): ## 把所有东西拼到 prompt 里
        tmp = self.prompt.replace('{conjecture_content}', self.conjecture.conjecture).replace('{proof_content}', self.conjecture.proof).replace('{review_content}', self.conjecture.review)

        if self.conjecture.dependencies and len(self.conjecture.dependencies) > 0: ## 说明有依赖, 不给依赖conjecture 的proof, 这点和AIM 保持一致
            i = 0
 
            deps = ''
 
            for dep in self.conjecture.dependencies:
                t = CONJECTURE_PREFIX.format(index = str(i), content = dep.conjecture)
                t = t + '\n'
                i += 1

                deps = deps + t

            context = CONTEXT_PREFIX.format(context_content = deps)
            tmp = tmp + '\n' + context

        return tmp


    def __load_refiner_prompt(self):
        
        f = open(self.prompt_file_path, 'r')
        prompt_template = f.read()

        return prompt_template


def create_refiner_agent(conjecture, model, prompt_file_path, shared_context):
    return Refiner(conjecture, model, prompt_file_path, shared_context) 
