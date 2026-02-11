from workflow import AlphaSolve
from utils.utils import load_prompt_from_file
from config.agent_config import AlphaSolveConfig

import os
import argparse

if __name__== "__main__" :


    default_max_worker_num = max(1, (os.cpu_count() or 1) - 2)

    parser = argparse.ArgumentParser(description="Run AlphaSolve.")

    ## 第一个参数: 迭代次数, 在每次迭代中间, orchestrator 会插入一次 lemma_pool 的清洗和合并(但是还没有实现)
    parser.add_argument("--iteration", type=int, default=1, help="Number of iteration (default: 1)")

    ## 第二个参数: 批量大小, 也就是多少个线程同时做 lemma 解题, 默认为 2
    parser.add_argument("--batch_size", type=int, default = 2, help="Number of parallel evolution process (default: num of cpu - 2)")
    
    ## 第三个参数: 模式, SHARED_BY_ALL = 所有 lemma 被所有 线程共享, SHARED_BY_ITERATION = 每个线程只看得到之前 iteration 的 lemma, 即本 iteration 的lemma 不共享
    parser.add_argument("--mode", type=str, default= AlphaSolveConfig.SHARED_BY_ALL, help="Output JSON file (default: auto name)")
    
    ## 第三个参数: 工具执行器的大小, 默认为 cpu - 2, 后续我们会把 tool 执行和 llm client 分开
    parser.add_argument("--tool_executor_size", type=int, default = default_max_worker_num, help="用来执行wolfram|python的进程池数量")
    
    args = parser.parse_args()

    problem = load_prompt_from_file('problems/problem_1.md')
    hint= None
    #hint = load_prompt_from_file('hint.md')
    
    alpha = AlphaSolve(problem=problem, max_worker_num = args.batch_size, tool_executor_size = args.tool_executor_size)
    
    try:
        solution = alpha.do_research(iteration_num = args.iteration)

        print("Final Solution:")
        print(solution)
    
        output_file = "solution.md"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("# Problem\n\n")
            f.write(f"```\n{problem}\n```\n\n")
            f.write("# Solution\n\n")
            f.write(solution)
    
        print(f"\nSolution has been saved to: {output_file}")
    finally:
        alpha.do_close()


