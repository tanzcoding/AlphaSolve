
from workflow import AlphaSolve


if __name__== "__main__" :

    print('[start] alpha solve agent ...')

    alpha = AlphaSolve(print_to_console=True)
    solution = alpha.do_research()

    print(solution)

    print('[exit] alpha solve agent ...')


