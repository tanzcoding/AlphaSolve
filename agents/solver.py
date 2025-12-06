import time
import json
import agents.conjecture_graph
import agents.shared_context

from agents.utils import build_conjuecture_helper
from agents.utils import load_prompt_from_file
from llms.kimi import KimiClient

CONTEXT_PREFIX = '''
## Context and History Explorations

Here is a list of context that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.

{context_content}
'''

CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'
DEPENDENCY_BEGIN = '\\begin{dependency}'
DEPENDENCY_END = '\\end{dependency}'


FINAL_BEGIN = '\\begin{final_proof}'
FINAL_END = '\\end{final_proof}'


class Solver:

    def __init__(self, llm, problem, model, prompt_file_path, shared_context):

        self.problem = problem
        self.model = model ## 代表模型的配置, 是一个string
        self.shared_context = shared_context 
        self.prompt_file_path = prompt_file_path
        self.prompt = load_prompt_from_file(self.prompt_file_path)
        self.llm = llm

    def solve(self, hint = None): ## 主方法, 可以扩展这个方法, 当前按照 AIM 的逻辑写的, 即每次生成 next-lemma, 但是对不对不管, 由后续的流程处理

        prompt = self.__build_solver_prompt(hint)

        b = time.time()
        
        resp = self.llm.get_result('', prompt)

        answer, cot = resp[0], resp[1] 

        answer = resp
        cot = ''

        print('using:', time.time() - b, len(answer), len(cot))

        print(answer)

        conj = self.__build_conjecture(answer, cot)

        return conj

    def build_context(self):
        return None


    def __build_solver_prompt(self, hint = None, with_failure_attempts = False):  ## 从文件中load prompt, 可以选择 step-by-step 的, 也可以用 AIM 原始的
         
        problem = self.problem
        context = self.build_context()
   
        failure_attemps = ''

        if with_failure_attempts:
            return None

        tmp = self.prompt.replace('{problem_content}', problem)

        if context:
            tmp = tmp + '\n' + CONTEXT_PREFIX.replace('{context_content}', context)    

        print('final solver prompt is: \n', tmp)
    
        return tmp


    def __build_conjecture(self, resp_from_llm, cot = None):
   
        conj = build_conjuecture_helper(resp_from_llm, CONJECTURE_BEGIN, CONJECTURE_END)
        proof = build_conjuecture_helper(resp_from_llm, PROOF_BEGIN, PROOF_END)

        dependencies = build_conjuecture_helper(resp_from_llm, DEPENDENCY_BEGIN, DEPENDENCY_END)
        data = [ ] 

        if not dependencies:
            data = json.loads(dependencies) ## 注意这货是个 json [ ... ] 
 
        is_theorem = False

        final_proof = build_conjuecture_helper(resp_from_llm, FINAL_BEGIN, FINAL_END)

        ## 情况一: 有 final proof, 那么有没有 conjecture / proof 还好
        ## 情况二: 都没有, 那就完犊子了, 返回空
        if not final_proof:
            if not conj or not proof:
                return None
        else: 
            proof = final_proof
            is_theorem = True

        conjecture = self.shared_context.add_to_conjecture_graph(conj, proof, data, is_theorem, cot)
      
        return conjecture
       

def create_solver_agent(problem, model, prompt_file_path, shared_context):
    
    kimi = KimiClient()
    return Solver(kimi, problem, model, prompt_file_path, shared_context)    
