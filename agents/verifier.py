import time
import json
import agents.conjecture_graph
from llms.volcano_ds import get_result

CONTEXT_PREFIX = '''
## Context and History Explorations

Here is a list of context that we have collected for this problem or our history findings during exploration. They serve as the background of the conjecture and proof and can be accepted without controversy as correct.

{context_content}
'''

CONJECTURE_PREFIX = '''
 ** Conjecture-{index} **
 {content}
'''

VERIFY_RESULT_VALID='boxed{valid}'
VERIFY_RESULT_INVALID='boxed{invalid}'


class Verifier:

    def __init__(self, problem, model, prompt_file_path, current_conj, reasoning_path, shared_context, init_context = None):
        self.problem = problem
        self.model = model
        self.prompt_file_path = prompt_file_path
        self.prompt = self.__load_solver_prompt()
        self.init_context = init_context
        self.current_conj = current_conj
        self.reasoning_path = reasoning_path
        self.shared_context = shared_context
 
    def verify(self): 
        prompt = self.__build_verifier_prompt(self.init_context, self.current_conj, self.reasoning_path)


        b = time.time()
 
        resp = get_result('', prompt)

        answer, cot = resp[0], resp[1]

        print('using:', time.time() - b, len(answer), len(cot))

        if VERIFY_RESULT_VALID in answer:
            return True, answer, cot
        else:
            return False, answer, cot


    def __extract_from_model(self, model_output):
        ## 从模型的返回中把信息提取出来, 返回是一个 tuple(conjucture, proof, final_proof, dependencies)
        return None

    def __build_verifier_prompt(self, context, conj, reasoning_path):
        ## 把所有东西拼到 prompt 里

        tmp = self.prompt.replace('{conjecture_content}', conj.conjecture).replace('{proof_content}', conj.proof)

        if reasoning_path and len(reasoning_path) > 0: ## 说明有依赖, 不给依赖conjecture 的proof, 这点和AIM 保持一致
            i = 0
 
            deps = ''
 
            for dep in reasoning_path:
                t = CONJECTURE_PREFIX.format(index = str(i), content = dep.conjecture)
                t = t + '\n'
                i += 1

                deps = deps + t

            context = CONTEXT_PREFIX.format(context_content = deps)
            tmp = tmp + '\n' + context

        return tmp


    def __load_solver_prompt(self):

        f = open(self.prompt_file_path, 'r')

        prompt_template = f.read()

        ## print('load prompt template ', self.prompt_file_path, ' with content ', prompt_template)

        return prompt_template


def create_verifier_agent(problem, model, prompt_file_path, conj, reasoning_path, shared_context):
    return Verifier(problem, model, prompt_file_path, conj, reasoning_path, shared_context) 
