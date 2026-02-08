from workflow import AlphaSolve
from utils.utils import load_prompt_from_file
from config.agent_config import AlphaSolveConfig

import os
import argparse

if __name__== "__main__" :


    default_max_worker_num = max(1, (os.cpu_count() or 1) - 2)

    parser = argparse.ArgumentParser(description="Run AlphaSolve benchmark.")
    parser.add_argument("--iteration", type=int, default=1, help="Number of iteration (default: 1)")
    parser.add_argument("--batch_size", type=int, default=default_max_worker_num, help="Number of parallel evolution process (default: num of cpu - 2)")
    parser.add_argument("--mode", type=str, default= AlphaSolveConfig.SHARED_BY_ALL, help="Output JSON file (default: auto name)")
    args = parser.parse_args()

    problem = load_prompt_from_file('problems/problem_1.md')
    hint= None
    #hint = load_prompt_from_file('hint.md')


    ## 现在是 batch_size 多少就多少个进程, 不过暂时分开, 不确定需不需要分开
    alpha = AlphaSolve(problem=problem, max_worker_num = args.batch_size)
    
    try:
        solution = alpha.do_research(batch_size = args.batch_size, iteration_num = args.iteration)

        print("Final Solution:")
        print(solution)
    
        # Write solution to markdown file, overwriting existing file

        output_file = "solution.md"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("# Problem\n\n")
            f.write(f"```\n{problem}\n```\n\n")
            f.write("# Solution\n\n")
            f.write(solution)
    
        print(f"\nSolution has been saved to: {output_file}")
    finally:
        alpha.do_close()


