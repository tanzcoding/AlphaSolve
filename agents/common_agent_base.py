import time
import json
import agents.conjecture_graph
import agents.shared_context

from agents.utils import build_conjuecture_helper
from agents.utils import load_prompt_from_file
from llms.kimi import KimiClient
from llms.deepseek import DeepSeekClient
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
    
    def __init__(self, llm, problem, model, prompt_file_path):
        super(Solver, self).__init__()
        self.problem = problem
        self.model = model
        self.prompt_file_path = prompt_file_path
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)

    def prep(self, shared): ## 按照 pocket-flow 的定义, 这一步是从 shard(一个dict) 里面拿出所有依赖

        shared_context = shared[AlphaSolveConfig.SHARED_CONTEXT]
        hint = shared[AlphaSolveConfig.HINT]

        iteration = shared[AlphaSolveConfig.TOTAL_SOLVER_ROUND]

        if iteration == 0:   ## solver 的迭代耗尽了
            print('[solver] solver quota exausted ...')
            return AlphaSolveConfig.SOLVER_EXAUSTED, shared_context, hint
        else:

            ## 这里需要 flush 一下 verifier 的quota
            print('[solver] flush verify and refine round to: ', )
            shared[AlphaSolveConfig.VERIFY_AND_REFINE_ROUND] = 
            return AlphaSolveConfig.NORMAL, shared_context, hint    
 
    def exec(self, prep_res): ## 执行主要的逻辑
  
        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        if len(prep_res) < 3:
            print('illegal prep_res with length: ', len(prep_res))
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        code = prep_res[0]

        if code == AlphaSolveConfig.SOLVER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED, None

        shared_context = prep_res[1]
        hint = prep_res[2] 

        conj = self.__solve(shared_context, hint)

        return AlphaSolveConfig.CONJECTURE_GENERATED, conj


    def post(self, shared, prep_res, exec_res):  ## 更新一下iteration 变量
  
        iteration = shared[AlphaSolveConfig.TOTAL_SOLVER_ROUND]
        iteration -= 1

        shared[AlphaSolveConfig.TOTAL_SOLVER_ROUND] = iteration

        if not exec_res or len(exec_res) == 0:
            return AlphaSolveConfig.EXIT_ON_ERROR

        if not exec_res[1]:
            return AlphaSolveConfig.EXIT_ON_ERROR

        conj = exec_res[1]

        print('[solver] putting conjecture into context: ', conj.conjecture)

        shared[AlphaSolveConfig.CURRENT_CONJECTURE] = conj

        return exec_res[0]


    def __solve(self, shared_context, hint):
        
        prompt = self.__build_solver_prompt(self.prompt_template, self.problem, shared_context, hint)

        b = time.time()

        resp = self.llm.get_result('', prompt)

        answer, cot = resp[0], resp[1]

        print('using:', time.time() - b, len(answer), len(cot))

        conj = self.__build_conjecture(shared_context, answer, cot)

        return conj


    def __build_solver_prompt(self, prompt_template, problem, shared_context, hint = None): 
         
        context = shared_context.build_context_by_lemma() 

        tmp = prompt_template.replace('{problem_content}', problem)

        if context:
            tmp = tmp + '\n' + CONTEXT_PREFIX.replace('{context_content}', context)    

        print('final solver prompt is: \n', tmp)
    
        return tmp


    def __build_conjecture(self, shared_context, resp_from_llm, cot = None):
   
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

        conjecture = shared_context.add_new_conjecture(conj, proof, data, cot, AlphaSolveConfig.SOLVER)
      
        return conjecture
       

def create_solver_agent(problem, model, prompt_file_path):
    
    ## kimi = KimiClient()
    ds = DeepSeekClient()
    return Solver(ds, problem, model, prompt_file_path)    
