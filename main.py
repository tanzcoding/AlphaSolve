from workflow import AlphaSolve
from utils.utils import load_prompt_from_file

if __name__== "__main__" :

    problem = load_prompt_from_file('problems/problem_1.md')
    hint= None
    #hint = load_prompt_from_file('hint.md')

    default_max_worker_num = max(1, (os.cpu_count() or 1) - 2)
    alpha = AlphaSolve(problem=problem, max_worker_num = default_max_worker_num)
    solution = alpha.do_research(batch_size = 2, iteration_num = 1)

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



