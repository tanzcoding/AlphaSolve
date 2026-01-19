from workflow import AlphaSolve
from utils.utils import load_prompt_from_file

if __name__== "__main__" :
    problem = load_prompt_from_file('problems/problem_4.md')
    hint= None
    #hint = load_prompt_from_file('hint.md')
    alpha = AlphaSolve(problem=problem, hint=hint, print_to_console=True)
    solution = alpha.do_research()

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



