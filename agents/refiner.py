import time
import json
from .shared_context import build_reasoning_path, Lemma, new_lemma
from utils.utils import extract_substring, load_prompt_from_file

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node

CONJECTURE_BEGIN = '<conjecture>'
CONJECTURE_END = '</conjecture>'
PROOF_BEGIN = '<proof>'
PROOF_END = '</proof>'

class Refiner(Node):

    def __init__(self, llm, prompt_file_path, logger): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
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
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        if shared["lemmas"][lemma_id].get("verify_round", 0) >= AlphaSolveConfig.MAX_VERIFY_AND_REFINE_ROUND:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, lemma_id, None, None

        lemma = shared["lemmas"][lemma_id]
        ctx_ids = build_reasoning_path(shared["lemmas"],lemma_id, verified_only=True)
        ctx_text = self.__render_context(ctx_ids, shared["lemmas"])

        prompt = self.__build_refiner_prompt(lemma, ctx_text)

        self.logger.log_print(
            f"event=context_built step=prep lemma_id={lemma_id} ctx_size={len(ctx_ids)}",
            module="refiner",
            print_to_console=self.print_to_console,
        )
        return AlphaSolveConfig.NORMAL, prompt, shared

    def exec(self, prep_res): 
        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR, None
        
        if len(prep_res) < 4:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        # Quota-exhausted path: prep() returns a short tuple; handle it before
        # validating the "normal" shape.
        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, True, None

        lemma_id, lemma, reasoning_ctx = prep_res[1], prep_res[2], prep_res[3]
        if not lemma.get("statement") or not lemma.get("proof"):
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        valid, new_lemma = self.__refine(lemma, reasoning_ctx)
        return AlphaSolveConfig.NORMAL, valid, new_lemma

    def post(self, shared, prep_res, exec_res): 
        # WRITE ONLY to shared here.
        if not prep_res or not exec_res:
            self.logger.log_print(
                "event=illegal_io step=post",
                module="refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            self.logger.log_print('exiting refiner...', module='refiner')
            return AlphaSolveConfig.EXIT_ON_ERROR

        if prep_res[0] == AlphaSolveConfig.VERIFIER_EXAUSTED:
            # verify-refine quota exhausted: we are abandoning the current lemma.
            # Bugfix: refund the solver lemma quota when the current lemma never
            # becomes verified after all verify-refine attempts.
            lemma_id = prep_res[1] if len(prep_res) > 1 else None
            if lemma_id is not None and 0 <= lemma_id < len(shared.get("lemmas", [])):
                lemma = shared["lemmas"][lemma_id]
                # Mark rejected so it won't be reused as context.
                lemma["status"] = "rejected"
                self.logger.log_print(
                    f"event=conjecture rejected and would not be refined again, step=post, lemma_id={lemma_id}",
                    module="refiner",
                    print_to_console=self.print_to_console,
                    level="WARNING",
                )
            self.logger.log_print('exiting refiner...', module='refiner')
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        if len(exec_res) < 3:
            self.logger.log_print(
                "event=illegal_exec_res step=post",
                module="refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            self.logger.log_print('exiting refiner...', module='refiner')
            return AlphaSolveConfig.EXIT_ON_ERROR

        lemma_id = prep_res[1]
        is_valid, next_lemma = exec_res[1], exec_res[2]

        if is_valid:
            if next_lemma:
                shared["lemmas"][lemma_id] = next_lemma  # in-place overwrite
                self.logger.log_print(
                    f"event=refine_success step=post lemma_id={lemma_id} remaining={shared['verify_refine_round_remaining']}",
                    module="refiner",
                    print_to_console=self.print_to_console,
                )
                self.logger.log_print('exiting refiner...', module='refiner')
                return AlphaSolveConfig.REFINE_SUCCESS
            self.logger.log_print(
                f"event=refine_no_output step=post lemma_id={lemma_id}",
                module="refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            self.logger.log_print('exiting refiner...', module='refiner')
            return AlphaSolveConfig.EXIT_ON_ERROR

        # refiner considers lemma wrong => route to solver
        shared["lemmas"][lemma_id]["status"] = "rejected"
        self.logger.log_print(
            f"event=lemma_wrong step=post lemma_id={lemma_id} remaining={shared['verify_refine_round_remaining']}",
            module="refiner",
            print_to_console=self.print_to_console,
            level="WARNING",
        )
        self.logger.log_print('exiting refiner...', module='refiner')
        return AlphaSolveConfig.CONJECTURE_WRONG
 

    def __refine(self, lemma, reasoning_ctx): 

        prompt = self.__build_refiner_prompt(lemma, reasoning_ctx)

        b = time.time()
        messages_to_send = [
            {"role": "user", "content": prompt}
        ]

        # For audit/debug/HTML: record the exact messages sent to LLM.
        self.logger.log_print(
            "event=llm_messages step=exec\n" + json.dumps(messages_to_send, ensure_ascii=False, indent=2),
            module="refiner",
            print_to_console=self.print_to_console,
        )

        answer, cot, _ = self.llm.get_result(messages_to_send)

        self.logger.log_print(
            f"event=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer)} cot_len={len(cot)}",
            module="refiner",
            print_to_console=self.print_to_console,
        )

        conj2, proof = self.__extract_from_model(answer)

        valid =  INVALID_TAG not in answer 

        if conj2 and proof:
            next_lemma = new_lemma(
                statement=conj2,
                proof=proof,
                cot=cot,
                status="pending",
                history_messages=lemma.get("history_messages", []),
                verify_round=lemma.get("verify_round", 0)+1,
            )
            # keep dependencies as-is (solver only uses verified lemmas anyway)
            return valid, next_lemma
        return valid, None


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
            "Here is a list of context that we have collected for this problem or our history findings during exploration. "
            "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
        )
        lines.append("")
        for i, lemma_id in enumerate(ctx_ids):
            lines.append(f" ** Conjecture-{i} **")
            lines.append(f" {lemmas[lemma_id].get('statement')}")
        return "\n".join(lines)


def create_refiner_agent(prompt_file_path, logger:Logger):
 
    llm = LLMClient(module='refiner', config=AlphaSolveConfig.REFINER_CONFIG, logger=logger)
    return Refiner(llm, prompt_file_path, logger=logger)
