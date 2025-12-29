
import agents.conjecture_graph


CONTEXT_PREFIX = '''
## Context and History Explorations

Here is a list of context that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.

{context_content}
'''

CONJECTURE_PREFIX = '''
 ** Conjecture-{index} **
 {content}
'''


class SharedContext:

    ## 整个 solver-verifier-refiner 循环的上下文, 各种和上下文相关的信息都会扔到这里来, 比如: AIM 的reasoning path, 比如 AIM 的 conjecture graph, 还有各种 refine 的 history
    ## 其实有三种 conjecture
    ### 第一种是正确的 conjecture
    ### 第二种是错误的 conjecture
    ### 第三种是错改对的 conjecture, 和 AIM 最的的区别是这种, 它们采用覆盖, 我们采用的是复制, 每一个 conj 有一个指针告诉我的 parent 是谁
    ## 因此我们会有2种结构:
    ### 第一种是 conjecture graph, 完整的 conjecture 集合
    ### 第二种是 submited_conjecture, 正确的集合

    def __init__(self):
        self.conjecture_graph = agents.conjecture_graph.ConjectureGraph() ## 管理所有的依赖关系
        self.submited_conjectures = [ ] ## 被标记为正确的 conjecture, 可以作为后续依赖

    def fetch_reasoning_path(self, conj, solved_only = True):  ## 给一条 lemma,  根据 dependency 把整个 reasoning tree 拉出来, 这个方法 verifier 会用到
        conjs = self.conjecture_graph.search_reasoning_path(conj, solved_only)
        return conjs
    
    def add_new_conjecture(self, conj, proof, dependencies, is_theorem, cot): ## generated_by 可以是 solver/可以是 refiner
        conjecture = self.conjecture_graph.add_to_conjecture_graph(conj, proof, dependencies, is_theorem, cot)
        
        return conjecture

    def add_to_conjecture_graph_by_parent(self, parent, conj, proof, cot):
        conjecture = self.conjecture_graph.add_to_conjecture_graph_by_parent(parent, conj, proof, cot)

        return conjecture

    def submit(self, conjecture):
        self.submited_conjectures.append(conjecture)

   
    def build_context_by_lemma(self): ## build solver 的 context, 把历史上正确的conjecture(lemma)全部喷回去
        
        if not self.submited_conjectures or len(self.submited_conjectures)  == 0:
            return None

        lemmas = ''       

        for i  in range(len(self.submited_conjectures)):
 
            conj = self.submited_conjectures[i]

            t = CONJECTURE_PREFIX.format(index = str(i), content = conj.conjecture)
            t = t + '\n'

            lemmas = lemmas + t

        context = CONTEXT_PREFIX.format(context_content = lemmas)

        return context
        

    def build_context_for_conjecture(self, conjecture, solved_only = True):

        reasoning_path = self.fetch_reasoning_path(conjecture, solved_only)

        if not reasoning_path or len(reasoning_path) == 0:
            return None

        deps = ''

        for i in range(reasoning_path):
            dep = reasoning_path[i]
            t = CONJECTURE_PREFIX.format(index = str(i), content = dep.conjecture)
            t = t + '\n'
            i += 1

            deps = deps + t

        context = CONTEXT_PREFIX.format(context_content = deps)
       
        return context
        
