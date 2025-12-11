
from workflow import AlphaSolve


if __name__== "__main__" :

    problem = 'Find all $\\alpha\\in\\mathbb{R}$ such that the equation $-\\alpha U(y)+\\left[(1+\\alpha)y+U(y)\\right] \\partial_y U(y) = 0$ has a solution in $\\{U\\in C^\\infty (\\mathbb{R}):U(0)=0\\}$ other than $U(y) = -y$ and $U(y)=0$.'

    print('[start] alpha solve agent ...')

    alpha = AlphaSolve(problem)
    solution, context = alpha.do_research()

    print('[exit] aplah solve agent ...')


