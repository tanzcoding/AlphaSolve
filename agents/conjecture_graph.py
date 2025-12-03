
class ConjectureGraph:

    def __init__(self):
        self.conjecture_graph = [ ]


    def add_to_conjecture_graph(self, conjecture, proof, dependencies, is_theorem = False, cot = None):

        index = len(self.conjecture_graph)        

        ## 转一下 dependenciess, 我们不用 index, 直接用读出来的 conjecture obj 代替, 这样后面写 reasoning path 的时候方便一些
        true_dependencies = [ ] 
        for index in dependencies:
            conj = self.__find_index(self.conjecture_graph, index) 
            if conj:
                true_dependencies.append(conj)

        conj = Conjecture(index, conjecture, proof, true_dependencies, is_theorem, cot)
        self.conjecture_graph.append(conj)

        return conj


    def search_reasoning_path(self, target_conj, solved_only = True): ## 注意其实 lemma 形成的是一个图结构, 我们强制弄成树结构(和AIM逻辑对齐)
      
        if not self.conjecture_graph: 
            return []

        result = [ ]
        ## result.append(target_conj)

        for conj in self.conjecture_graph:
            if conj.index == target_conj.index: ## 找到目标节点
                self.__search_sub_conj_graph(conj, result, solved_only)
                ## 不太可多个, 加个 break 保险
                break

        return result


    def __search_sub_conj_graph(self, current, res, solved_only): ## 注意 conj_graph 是一个图结构, 但是我们除了成树结构(重复节点不重复插入) 这点和 AIM 一致
      
        if not current.dependencies: # 叶子节点了
            return

        for t in current.dependencies:
            if self.__find_index(res, t.index): ## 已经有了, 去重
                continue
            if correct_proof_only:
                if t.solved:
                    res.append(t)
                    self.__search_sub_conj_graph(lemma, res, solved_only) ## 继续递归
            else:
                res.append(t)
                self.__search_sub_conj_graph(lemma, res, solved_only) ## 继续递归


    def __find_index(self, container, index):

        for conj in container:
            if conj.index == index:
                return conj

        return None


class Conjecture:
    
    def __init__(self, index, conjecture, proof, dependencies, is_theorem = False, cot = None, next_conjecture = None):
        self.index = index
        self.conjecture = conjecture
        self.proof = proof
        self.proof_summary = None
        self.review = None
        self.comment = None
        self.solved = False
        self.is_theorem = is_theorem
        self.dependencies = dependencies
        self.cot = cot
        self.next_conjecture = next_conjecture

    def add_proof_summary(self):
        return None


    def create_sub(self, conjecture, proof):
        sub = Conjecture(self.index, conjecture, proof, self.dependencies, self.is_theorem, self.is_theorem)
        self.next_conjecture = sub

        return sub

