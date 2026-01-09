import time
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger
from .shared_context import SharedContext, Lemma
from pocketflow import Node


class WithHistoryRefiner(Node):
    """A refiner that DOES expose solver history (history_messages) along with the review.

    Contract:
    - It sends the LLM: shared['lemmas'][shared['current_lemma_id']] + appended verifier review.
    - In the exec function, LLM modifies the statement/proof of the lemma using modify_lemma tool
    - In the post function, the updated messages from exec are stored back to shared['lemmas'][shared['current_lemma_id']]['history_messages'] for next round.
    """

    def __init__(self, llm: LLMClient, logger: Logger):
        super(WithHistoryRefiner, self).__init__()
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

        diff_instructions = """Repair the current conjecture statement and/or proof so they satisfy the review. Focus on the mathematics first; editing tools are just the delivery mechanism.

Workflow:
1. Read the review carefully and decide the precise edits needed in the statement and/or proof.
2. You MUST call one (or both) of the editing tools to make changes:
   - `modify_statement` (provide `new_statement`) to replace the entire conjecture statement.
   - `modify_proof` (provide `begin_marker`, `end_marker`, `proof_replacement`) to replace a span of the proof.
   Direct text replies never modify the conjecture—only tool calls do.

Proof-anchor reminder:
- `begin_marker` and `end_marker` are inclusive: both are removed along with the replaced span unless reintroduced in `proof_replacement`.
- Each marker must be ≤50 characters and should appear verbatim in the current proof.
- Make incremental edits; you can call `modify_proof` multiple times.

After successfully refining the conjecture/proof, briefly summarize what changed (no follow-up questions)."""

        messages_to_send.append(
            {
                "role": "user",
                "content": (
                    diff_instructions + "\n\n"
                    "<review>\n" + lemma.get("review", "") + "\n</review>\n"
                ),
            }
        )

        original_length = len(messages_to_send)
        _, _, updated_messages = self.llm.get_result(messages_to_send, shared=shared)

        # Check if apply_diff tool was used in the newly generated messages
        used_apply_diff = False
        new_messages = updated_messages[original_length:]  # Only check new messages
        for msg in new_messages:
            if msg.get('role') == 'assistant' and msg.get('tool_calls'):
                for tool_call in msg['tool_calls']:
                    if tool_call.get('function', {}).get('name') == 'apply_diff':
                        used_apply_diff = True
                        break
            if used_apply_diff:
                break

        # If apply_diff was not used, revert to original messages
        if not used_apply_diff:
            self.logger.log_print(
                "event=apply_diff_not_used, reverting to original messages",
                module="diff_refiner",
                level="WARNING"
            )
            updated_messages = lemma.get("history_messages", [])

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
                module="diff_refiner",
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


def create_with_history_refiner_agent(logger: Logger):
    llm = LLMClient(module='with_history_refiner', config=AlphaSolveConfig.WITH_HISTORY_REFINER_CONFIG, logger=logger)
    return WithHistoryRefiner(llm, logger=logger)
