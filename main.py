from workflow import AlphaSolve
from agents.utils import load_prompt_from_file

if __name__== "__main__" :
    problem = load_prompt_from_file('problem1.md')
    alpha = AlphaSolve(problem=problem,print_to_console=True)
    solution = alpha.do_research()

    print("Final Solution:")
    print(solution)



