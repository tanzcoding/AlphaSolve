import os, time, threading, random

from config.agent_config import AlphaSolveConfig
from agents.shared_context import SharedContext
from openai import OpenAI
from agents.solver import create_solver_agent
from agents.verifier import create_verifier_agent
from agents.refiner import create_refiner_agent

class AlphaSolve:
    ## 首先, AIM 其实没什么东西可以复用的,整体上目前大部分Math Aget 都可以总结成, solve(explorer)-verify(reviewer)-refine 的模式
    ## 但是具体做看实验了, 因此 AlphaSolve 的主类仅封装: (1) solve(explorer)-verify(reviewer)-refine 的模式 (2) solve & verify & refine 的历史 trace

    def __init__(self, problem):
        self.problem = problem
        self.shared_context = SharedContext()

    def do_solve(self):
        solver = create_solver_agent(self.problem,  AlphaSolveConfig.SOLVER_MODEL, AlphaSolveConfig.SOLVER_PROMPT_PATH, self.shared_context)
        conj = solver.solve()  
        reasoning_path = solver.fetch_reasoning_path(conj, False)

        return conf, reasoning_path


    def do_verify(target_conj, reasoning_path):
        ## test time compute, 我们先直接撸3次, 任何一次错我们都认为错, 随机选择一个判错的 review 和 cot —— 这里和AIM不一样
        verifier_res = None

        result = [ ] 

        verifier = create_verifier_agent(problem, AlphaSolveConfig.VERIFIER_MODEL, AlphaSolveConfig.VERIFIER_PROMPT_PATH, target_conj, reasoning_path, self.shared_context)
        for i in range(AlphaSolveConfig.VERIFIER_TEST_TIME_ROUND):
            is_valid, review, cot = verifier.verify()
            print('verifier test:', i, is_valid)
 
            if not is_valid:
                result.append(is_valid, review, cot)       
            else:
                verifier_res = is_valid, review, cot

        if len(result) > 0:
            ## 说明有错误, 
            is_valid, review, cot = random.randint(0, len(result))
            return is_valid, review, cot

        else: 
            ## 说明全对了, 返回最后一次的结果
            return verifier_res

    def do_refine(self, target_conj):

        refiner = create_refiner_agent(conj, AlphaSolveConfig.REFINE_MODEL, AlphaSolveConfig.REFINE_PROMPT_PATH, self.shared_context)
                
        valid, refined_conj = refiner.refine()

        if valid: ## 到这里说明引理是对的
            return valid, refined_conj

        else: ## 到这里说明引理错了, 那么需要有改进的 conj
            if not refind_conj: ## 比较奇怪的边界场景, 一般是 LLM 不遵循指令的幻觉
                print('instruction not followed, refind_conj not exist ', self.problem)
                return valid, None

            return valid, refined_conj
            
    def do_research(self):  ## 主类入口

        last_conj = None

        for i in range(AlphaSolveConfig.TOTAL_SOLVER_ROUND): ## 总循环迭代次数
            conj = self.do_solve() ## 生成 next-lemma

            for i in range(AlphaSolveConfig.VERIFY_AND_REFINE_ROUND): ## 做 verify-refine 的轮次
                is_valid, review, cot = self.do_verify(conj)
                
                if is_valid:  ## 提交到上下文里面, 让后续都能依赖
                    conj.is_solved = True
                    self.shared_context.submit(conj, review, cot)
                    break

                else:
                    refined_conj = self.do_refine(conj)
                    conj = refined_conj

            last_conj = conj
                                        
            if last_conj.is_solved and last_conf.final_proof:
                print('problem solved ...')
                break           
 
        return last_conj.conjecture, last_conj.proof


if __name__== "__main__" :
 
    problem = 'Find all $\\alpha\\in\\mathbb{R}$ such that the equation $-\\alpha U(y)+\\left[(1+\\alpha)y+U(y)\\right] \\partial_y U(y) = 0$ has a solution in $\\{U\\in C^\\infty (\\mathbb{R}):U(0)=0\\}$ other than $U(y) = -y$ and $U(y)=0$.'


    print('[start] alpha solve agent ...')
    
    alpha = AlphaSolve(problem)
    solution, context = alpha.do_research()

    print('[exit] aplah solve agent ...')


