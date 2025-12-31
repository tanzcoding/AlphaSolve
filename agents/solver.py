import time
import json
import agents.conjecture_graph
from agents.shared_context import CONTEXT_PREFIX

from agents.utils import build_conjuecture_helper
from agents.utils import load_prompt_from_file
from llms.utils import LLMClient
from config.agent_config import AlphaSolveConfig
from utils.logger import log_print

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
    
    def __init__(self, llm, problem, prompt_file_path, print_to_console):
        super(Solver, self).__init__()
        self.problem = problem
        self.prompt_file_path = prompt_file_path
        self.llm = llm
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.print_to_console = print_to_console

    def prep(self, shared): ## 按照 pocket-flow 的定义, 这一步是从 shard(一个dict) 里面拿出所有依赖
        ## 处理异常情况
        iteration = shared[AlphaSolveConfig.TOTAL_SOLVER_ROUND]
        if iteration == 0:   ## solver 的迭代耗尽了
            log_print('[solver] solver quota exausted ...', print_to_console=self.print_to_console)
            return AlphaSolveConfig.SOLVER_EXAUSTED, None, None, self.print_to_console
        
        shared_context = shared[AlphaSolveConfig.SHARED_CONTEXT]
        hint = shared[AlphaSolveConfig.HINT]

        print_to_console = shared[AlphaSolveConfig.PRINT_TO_CONSOLE]

        # iteration 表示当前还剩多少次 solver 可以生成新的引理/猜想
        remaining_lemma_quota = iteration
        prompt = self.__build_solver_prompt(
            self.prompt_template,
            self.problem,
            shared_context,
            remaining_lemma_quota=remaining_lemma_quota,
            hint=hint,
        )
        messages_to_send = [
            {"role": "user", "content": prompt}
        ]
        return AlphaSolveConfig.NORMAL, messages_to_send, shared_context, self.print_to_console  
        
 
    def exec(self, prep_res): ## 执行主要的逻辑
        ## 处理异常情况
        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        if len(prep_res) < 4:
            log_print('illegal prep_res with length: ', len(prep_res), print_to_console=self.print_to_console)
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        code = prep_res[0]

        if code == AlphaSolveConfig.SOLVER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED, None

        b = time.time()
        messages = prep_res[1]
        shared_context = prep_res[2]
        print_to_console = prep_res[3]
        # Solver 可使用工具（已在配置中设置）
        answer, cot = self.llm.get_result(messages, print_to_console=print_to_console)

        log_print(f'[solver] using: {time.time() - b:.1f}s, answer length: {len(answer)}, cot length: {len(cot)}', print_to_console=self.print_to_console)

        conj = self.__build_conjecture(shared_context, answer, cot)

        return AlphaSolveConfig.CONJECTURE_GENERATED, conj


    def post(self, shared, prep_res, exec_res):  ## 更新一下iteration 变量
        
        ## 处理异常情况
        if not exec_res or len(exec_res) == 0:
            log_print('[solver] illegal exec_res in solver post', print_to_console=self.print_to_console)
            return AlphaSolveConfig.EXIT_ON_ERROR

        #处理solver步数耗尽
        if exec_res[0] == AlphaSolveConfig.EXIT_ON_EXAUSTED:
            log_print('[solver] solver exhausted during post ...', print_to_console=self.print_to_console)
            return AlphaSolveConfig.EXIT_ON_EXAUSTED


        # 不知道为什么没有生成引理, 直接重新开始
        if not exec_res[1]:
            log_print('[solver] no conjecture generated ...', print_to_console=self.print_to_console)
            return AlphaSolveConfig.EXIT_ON_ERROR
        
        
        iteration = shared[AlphaSolveConfig.TOTAL_SOLVER_ROUND]
        iteration -= 1

        shared[AlphaSolveConfig.TOTAL_SOLVER_ROUND] = iteration

        ## 重新打满 verifier-refiner round
        shared[AlphaSolveConfig.VERIFY_AND_REFINE_ROUND] = AlphaSolveConfig.VERIFY_AND_REFINE_ROUND_NUM

        conj = exec_res[1]

        log_print('[solver] putting conjecture into context: ', conj.conjecture, print_to_console=self.print_to_console)

        shared[AlphaSolveConfig.CURRENT_CONJECTURE] = conj

        log_print('[solver] solver generated new conjecture ...', print_to_console=self.print_to_console)
        return AlphaSolveConfig.CONJECTURE_GENERATED


    def __build_solver_prompt(self, prompt_template, problem, shared_context, remaining_lemma_quota, hint = None): 
         
        context = shared_context.build_context_by_lemma() 

        tmp = prompt_template.replace('{problem_content}', problem)

        # 将 prompt 中的剩余引理预算占位符替换成当前剩余次数
        tmp = tmp.replace('{remaining_lemma_quota}', str(remaining_lemma_quota))

        if context:
            tmp = tmp + '\n' + CONTEXT_PREFIX.replace('{context_content}', context)    

        log_print('final solver prompt is: \n', tmp, print_to_console=self.print_to_console)
    
        return tmp


    def __build_conjecture(self, shared_context, resp_from_llm, cot = None):
   
        conj = build_conjuecture_helper(resp_from_llm, CONJECTURE_BEGIN, CONJECTURE_END)
        proof = build_conjuecture_helper(resp_from_llm, PROOF_BEGIN, PROOF_END)

        dependencies = build_conjuecture_helper(resp_from_llm, DEPENDENCY_BEGIN, DEPENDENCY_END)
        data = [ ] 

        if dependencies:
            data = json.loads(dependencies) ## 注意这货是个 json [ ... ] 
 
        is_theorem = False

        final_proof = build_conjuecture_helper(resp_from_llm, FINAL_BEGIN, FINAL_END)

        ## 情况一: 有 final proof, 那么有没有 conjecture / proof 还好
        ## 情况二: 都没有, 那就完犊子了, 返回空
        if not final_proof:
            if not conj or not proof:
                return None
        else:
            # 如果给出最终证明，则将 conjecture 设置为原始问题陈述
            conj = self.problem
            proof = final_proof
            is_theorem = True

        conjecture = shared_context.add_new_conjecture(conj, proof, data, is_theorem, cot)
      
        return conjecture
       

def create_solver_agent(problem, prompt_file_path, print_to_console):
    
    llm = LLMClient(AlphaSolveConfig.SOLVER_CONFIG, print_to_console=print_to_console)
    return Solver(llm, problem, prompt_file_path, print_to_console)
