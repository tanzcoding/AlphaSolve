from .shared_context import build_reasoning_path, Lemma, new_lemma, save_snapshot
from utils.utils import extract_substring, load_prompt_from_file

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node

CONJECTURE_BEGIN = r'\begin{conjecture}'
CONJECTURE_END = r'\end{conjecture}'
PROOF_BEGIN = r'\begin{proof}'
PROOF_END = r'\end{proof}'

class Refiner(Node):

    def __init__(self, llm: LLMClient, prompt_file_path, logger): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Refiner, self).__init__()
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm
        self.logger = logger

    def prep(self,shared): 
        # READ ONLY from shared here.
        self.logger.log_print('entering refiner...', module='refiner')

        lemma_id = shared.get("current_lemma_id")
        if lemma_id is None:
            self.logger.log_print(
                "event=no_current_lemma step=prep",
                module="refiner",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR, None, None

        if shared["lemmas"][lemma_id].get("verify_round", 0) >= AlphaSolveConfig.MAX_VERIFY_AND_REFINE_ROUND:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, None, None

        lemma = shared["lemmas"][lemma_id]
        ctx_ids = build_reasoning_path(shared["lemmas"],lemma_id, verified_only=True)
        ctx_text = self.__render_context(ctx_ids, shared["lemmas"])

        prompt = self.__build_refiner_prompt(lemma, ctx_text)
        messages_to_send = [
            {"role": "user", "content": prompt}
        ]

        self.logger.log_print(
            f"event=context_built step=prep lemma_id={lemma_id} ctx_size={len(ctx_ids)}",
            module="refiner",
        )
        return AlphaSolveConfig.NORMAL, messages_to_send, shared

    def exec(self, prep_res): 
        if not prep_res or len(prep_res) < 3:
            return AlphaSolveConfig.EXIT_ON_ERROR

        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, None, None

        messages_to_send = prep_res[1]
        shared = prep_res[2]
        for attempt in range(AlphaSolveConfig.REFINER_MAX_RETRY):
            response,_,_ = self.llm.get_result(messages=messages_to_send,shared=shared)
            if self.__validate_response(response):
                break
            self.logger.log_print(
                "event=invalid_response, try refine again, step=exec",
                module="refiner",
                level="WARNING",
            )
        new_statement, new_proof = self.__extract_from_model(response)

        return AlphaSolveConfig.NORMAL, new_statement, new_proof

    def post(self, shared, prep_res, exec_res):
        # WRITE ONLY to shared here.
        if len(exec_res) < 3 or exec_res[0] == AlphaSolveConfig.EXIT_ON_ERROR:
            self.logger.log_print(
                "event=illegal_io step=post",
                module="refiner",
                level="ERROR",
            )
            self.logger.log_print('exiting refiner...', module='refiner')
            save_snapshot(shared, "refiner", AlphaSolveConfig.EXIT_ON_ERROR)
            return AlphaSolveConfig.EXIT_ON_ERROR

        if exec_res[0] >= AlphaSolveConfig.VERIFIER_EXAUSTED:
            lemma_id = shared.get("current_lemma_id")
            if lemma_id is not None and 0 <= lemma_id < len(shared.get("lemmas", [])):
                lemma = shared["lemmas"][lemma_id]
                # Mark rejected so it won't be reused as context.
                lemma["status"] = "rejected"
                self.logger.log_print(
                    f"event=conjecture rejected and would not be refined again, step=post, lemma_id={lemma_id}",
                    module="refiner",
                    level="WARNING",
                )
            self.logger.log_print('exiting refiner...', module='refiner')
            save_snapshot(shared, "refiner", AlphaSolveConfig.EXIT_ON_EXAUSTED)
            return AlphaSolveConfig.EXIT_ON_EXAUSTED
        
        new_statement = exec_res[1]
        new_proof = exec_res[2]

        refine_success = False
        
        if new_statement and len(new_statement)>5:
            shared["lemmas"][shared["current_lemma_id"]]["statement"] = new_statement
            refine_success = True
            self.logger.log_print(
                f"event=statement updated:\n<conjecture>{new_statement}\n</conjecture>",
                module="refiner",
            )
        if new_proof and len(new_proof)>5:
            shared["lemmas"][shared["current_lemma_id"]]["proof"] = new_proof
            refine_success = True
            self.logger.log_print(
                f"event=proof updated:\n<proof>{new_proof}\n</proof>",
                module="refiner",
            )
        if not refine_success:
            self.logger.log_print(
                "event=refiner_no_output step=exec",
                module="refiner",
                level="ERROR",
            )
            save_snapshot(shared, "refiner", AlphaSolveConfig.EXIT_ON_ERROR)
            return AlphaSolveConfig.EXIT_ON_ERROR
        
        save_snapshot(shared, "refiner", AlphaSolveConfig.REFINE_SUCCESS)
        return AlphaSolveConfig.REFINE_SUCCESS

    def __validate_response(self, response):
        if self.__has_unique_conjecture(response) and self.__has_unique_proof(response):
            return True
        return False

    def __has_unique_conjecture(self, response):
        begin_count = response.count(CONJECTURE_BEGIN)
        end_count = response.count(CONJECTURE_END)
        if not (begin_count == 1 and end_count == 1):
            return False
        begin_index = response.find(CONJECTURE_BEGIN)
        end_index = response.find(CONJECTURE_END)
        if not (begin_index < end_index):
            return False
        return True

    def __has_unique_proof(self, response):
        begin_count = response.count(PROOF_BEGIN)
        end_count = response.count(PROOF_END)
        if not (begin_count == 1 and end_count == 1):
            return False
        begin_index = response.find(PROOF_BEGIN)
        end_index = response.find(PROOF_END)
        if not (begin_index < end_index):
            return False
        return True

    def __extract_from_model(self, model_output):
        
        conj = extract_substring(
            model_output,
            CONJECTURE_BEGIN,
            CONJECTURE_END,
            logger=self.logger,
            module="refiner",
        )
        proof = extract_substring(
            model_output,
            PROOF_BEGIN,
            PROOF_END,
            logger=self.logger,
            module="refiner",
        )        

        return conj, proof

    def __build_refiner_prompt(self, lemma, reasoning_ctx): ## 把所有东西拼到 prompt 里

        if not lemma.get("statement") or not lemma.get("proof"):
            return None

        tmp = self.prompt_template.replace('{conjecture_content}', lemma.get("statement", "")).replace('{proof_content}', lemma.get("proof", ""))

        if lemma.get("review"):
            tmp = tmp.replace('{review_content}', lemma.get("review"))

        if reasoning_ctx:
            tmp = tmp + '\n' + reasoning_ctx

        return tmp

    def __render_context(self, ctx_ids, lemmas):
        if not ctx_ids:
            return None
        lines = []
        lines.append("## Context and History Explorations")
        lines.append("")
        lines.append(
            "Here is a list of lemma that we have collected for this problem or our history findings during exploration. "
            "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
        )
        lines.append("")
        for i, lemma_id in enumerate(ctx_ids):
            lines.append(f" ** Lemma-{i} **")
            lines.append(f" {lemmas[lemma_id].get('statement')}")
        return "\n".join(lines)


def create_refiner_agent(prompt_file_path, logger:Logger):
 
    llm = LLMClient(module='refiner', config=AlphaSolveConfig.REFINER_CONFIG, logger=logger)
    return Refiner(llm, prompt_file_path, logger=logger)
