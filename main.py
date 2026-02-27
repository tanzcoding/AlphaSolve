from workflow import AlphaSolve
from utils.utils import load_prompt_from_file
from config.agent_config import AlphaSolveConfig
from utils.log_session import LogSession

import os
import argparse

if __name__== "__main__" :

    default_max_worker_num = max(1, (os.cpu_count() or 1) - 2)

    parser = argparse.ArgumentParser(description="Run AlphaSolve.")
     
    ## 第三个参数: 工具执行器的大小, 默认为 cpu - 2, 后续我们会把 tool 执行和 llm client 分开
    parser.add_argument("--tool_executor_size", type=int, default = default_max_worker_num, help="用来执行wolfram|python的进程池数量")
    
    args = parser.parse_args()

    problem = load_prompt_from_file('problems/problem_1.md')
    hint= None
    #hint = load_prompt_from_file('hint.md')
    
    log_session = LogSession(run_root=AlphaSolveConfig.LOG_PATH)
    alpha = AlphaSolve(
        problem=problem,
        tool_executor_size=args.tool_executor_size,
        log_session=log_session,
    )
    
    try:
        solution = alpha.do_research()

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


