import time
import json

from agents.utils import build_conjecture_helper
from agents.utils import load_prompt_from_file

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node

## 一旦出现这条标签, 说明 lemma 是错的
INVALID_TAG = '\\boxed{false}'
CONJECTURE_BEGIN = '\\begin{conjecture}'
CONJECTURE_END = '\\end{conjecture}'
PROOF_BEGIN = '\\begin{proof}'
PROOF_END = '\\end{proof}'

class Refiner(Node):

    def __init__(self, llm, prompt_file_path, logger): ## reasoning path 是依赖的, 状态=solved 的引理, 作为上下文
        super(Refiner, self).__init__()
        self.prompt_file_path = prompt_file_path
        self.prompt_template = load_prompt_from_file(prompt_file_path)
        self.llm = llm
        self.logger = logger
        self.print_to_console = logger.print_to_console_default

    def prep(self,shared): 
        # READ ONLY from shared here.
        iteration = shared["verify_refine_round_remaining"]
        if iteration == 0:
            # Pass lemma_id through prep_res so post() can handle quota accounting
            # without needing to read shared.
            return AlphaSolveConfig.VERIFIER_EXAUSTED, shared.get("current_lemma_id")

        lemma_id = shared["current_lemma_id"]
        if lemma_id is None:
            self.logger.log_print(
                "event=no_current_lemma step=prep",
                module="refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        lemma = shared["lemmas"][lemma_id]
        ctx_ids = shared.build_reasoning_path(lemma_id, verified_only=True)
        ctx_text = self.__render_context(ctx_ids, shared["lemmas"])

        self.logger.log_print(
            f"event=context_built step=prep lemma_id={lemma_id} ctx_size={len(ctx_ids)} remaining={iteration}",
            module="refiner",
            print_to_console=self.print_to_console,
        )
        return AlphaSolveConfig.NORMAL, lemma_id, lemma, ctx_text

    def exec(self, prep_res): 
        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        # Quota-exhausted path: prep() returns a short tuple; handle it before
        # validating the "normal" shape.
        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, True, None

        if len(prep_res) < 4:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

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
            return AlphaSolveConfig.EXIT_ON_ERROR

        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            # verify-refine quota exhausted: we are abandoning the current lemma.
            # Bugfix: refund the solver lemma quota when the current lemma never
            # becomes verified after all verify-refine attempts.
            lemma_id = prep_res[1] if len(prep_res) > 1 else None
            if lemma_id is not None and 0 <= lemma_id < len(shared.get("lemmas", [])):
                lemma = shared["lemmas"][lemma_id]
                # Mark rejected so it won't be reused as context.
                lemma["status"] = "rejected"

                # Refund only once per lemma (in case of unexpected re-entry).
                if not lemma.get("solver_round_refunded"):
                    lemma["solver_round_refunded"] = True
                    before = shared["solver_round_remaining"]
                    shared["solver_round_remaining"] = min(
                        AlphaSolveConfig.SOLVER_ROUND_NUM,
                        shared["solver_round_remaining"] + 1,
                    )
                    self.logger.log_print(
                        f"event=verify_refine_exhausted_refund step=post lemma_id={lemma_id} solver_round_remaining={before}->{shared['solver_round_remaining']}",
                        module="refiner",
                        print_to_console=self.print_to_console,
                        level="WARNING",
                    )
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        if len(exec_res) < 3:
            self.logger.log_print(
                "event=illegal_exec_res step=post",
                module="refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        # decrement verify/refine rounds
        shared["verify_refine_round_remaining"] = shared["verify_refine_round_remaining"] - 1

        lemma_id = prep_res[1]
        is_valid, next_lemma = exec_res[1], exec_res[2]

        if is_valid:
            if next_lemma:
                shared["lemmas"][lemma_id] = next_lemma  # in-place overwrite
                shared["current_lemma_id"] = lemma_id
                self.logger.log_print(
                    f"event=refine_success step=post lemma_id={lemma_id} remaining={shared['verify_refine_round_remaining']}",
                    module="refiner",
                    print_to_console=self.print_to_console,
                )
                return AlphaSolveConfig.REFINE_SUCCESS
            self.logger.log_print(
                f"event=refine_no_output step=post lemma_id={lemma_id}",
                module="refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        # refiner considers lemma wrong => route to solver
        shared["lemmas"][lemma_id]["status"] = "rejected"
        self.logger.log_print(
            f"event=lemma_wrong step=post lemma_id={lemma_id} remaining={shared['verify_refine_round_remaining']}",
            module="refiner",
            print_to_console=self.print_to_console,
            level="WARNING",
        )
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

        answer, cot = self.llm.get_result(messages_to_send)

        self.logger.log_print(
            f"event=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer)} cot_len={len(cot)}",
            module="refiner",
            print_to_console=self.print_to_console,
        )

        conj2, proof = self.__extract_from_model(answer)

        valid =  INVALID_TAG not in answer 

        if conj2 and proof:
            next_lemma = {
                **lemma,
                "statement": conj2,
                "proof": proof,
                "cot": cot,
                "status": "pending",
            }
            # keep dependencies as-is (solver only uses verified lemmas anyway)
            return valid, next_lemma
        return valid, None


    def __extract_from_model(self, model_output):
        
        conj = build_conjecture_helper(
            model_output,
            CONJECTURE_BEGIN,
            CONJECTURE_END,
            logger=self.logger,
            module="refiner",
        )
        proof = build_conjecture_helper(
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
 
    llm = LLMClient(AlphaSolveConfig.REFINER_CONFIG, logger=logger)
    return Refiner(llm, prompt_file_path, logger=logger)
