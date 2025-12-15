
from workflow import AlphaSolve


if __name__== "__main__" :

    print('[start] alpha solve agent ...')

    alpha = AlphaSolve()
    solution, context = alpha.do_research()

    print('[exit] alpha solve agent ...')


