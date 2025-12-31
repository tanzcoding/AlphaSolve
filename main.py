from workflow import AlphaSolve
from utils.logger import log_print


if __name__== "__main__" :

    log_print('[start] alpha solve agent ...')

    alpha = AlphaSolve(print_to_console=True)
    solution = alpha.do_research()

    log_print(solution)

    log_print('[exit] alpha solve agent ...')



