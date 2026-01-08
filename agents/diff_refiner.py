import time
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger
from .shared_context import SharedContext, Lemma
from pocketflow import Node


# If this tag appears, the refiner believes the conjecture/lemma is false.



class DiffRefiner(Node):
    """A refiner that only accept apply unified diffs to modify unverified conjectures.

    Contract:
    - It sends the LLM: shared['lemmas'][shared['current_lemma_id']] + appended verifier review.
    - In the exec function, LLM modifies the statement/proof of the lemma using modify_lemma tool
    - In the post function, the updated messages from exec are stored back to shared['lemmas'][shared['current_lemma_id']]['history_messages'] for next round.
    """

    def __init__(self, llm: LLMClient, logger: Logger):
        super(DiffRefiner, self).__init__()
        self.llm = llm
        self.logger = logger

    def prep(self, shared: SharedContext):
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
            # Pass lemma_id through prep_res so post() can handle quota accounting
            # without needing to read shared.
            return AlphaSolveConfig.VERIFIER_EXAUSTED, lemma_id, None, None

        lemma = shared["lemmas"][lemma_id]

        # Pass shared context to exec so apply_diff can access current_lemma_id
        return AlphaSolveConfig.NORMAL, lemma, shared

    def exec(self, prep_res):
        if prep_res[0] == AlphaSolveConfig.VERIFIER_EXAUSTED:
            return AlphaSolveConfig.EXIT_ON_EXAUSTED, None
        lemma = prep_res[1]
        shared = prep_res[2]
        messages_to_send = lemma.get("history_messages", [])
        messages_to_send.append(
            {
                "role": "user",
                "content": "Here is the verifier's review of your conjecture/proof. Please refine the conjecture/proof using the apply_diff tool. \n\n<review>" + lemma.get("review", "") + "</review>\n",
            }
        )
        _, _, updated_messages = self.llm.get_result(messages_to_send, shared=shared)
        return AlphaSolveConfig.NORMAL, updated_messages

    def post(self, shared, prep_res, exec_res):
        # WRITE ONLY to shared here.
        if not prep_res or not exec_res:
            self.logger.log_print(
                "event=illegal_io step=post",
                module="diff_refiner",
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        if prep_res[0] == AlphaSolveConfig.VERIFIER_EXAUSTED:
            # verify-refine quota exhausted: we are abandoning the current lemma.
            # Bugfix: refund the solver lemma quota when the current lemma never
            # becomes verified after all verify-refine attempts.
            # In the post function, we should get lemma_id like this:
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
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        if len(exec_res) < 2:
            self.logger.log_print(
                "event=illegal_exec_res step=post",
                module="refiner",
                level="ERROR",
            )
            self.logger.log_print('exiting refiner...', module='refiner')
            return AlphaSolveConfig.EXIT_ON_ERROR

        # Get lemma_id from shared and update lemma status and history messages
        lemma_id = shared.get("current_lemma_id")
        shared["lemmas"][lemma_id]["status"] = "pending"
        updated_messages = exec_res[1]
        shared["lemmas"][lemma_id]["history_messages"] = updated_messages
        
        return AlphaSolveConfig.REFINE_SUCCESS


def create_diff_refiner_agent(logger: Logger):
    llm = LLMClient(module='diff_refiner', config=AlphaSolveConfig.DIFFREFINER_CONFIG, logger=logger)
    return DiffRefiner(llm, logger=logger)

