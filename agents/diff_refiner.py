import time
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger

from pocketflow import Node


# If this tag appears, the refiner believes the conjecture/lemma is false.
INVALID_TAG = "\\boxed{false}"


# XML-like tags for diff-refinement protocol.
CONJECTURE_TAG_BEGIN = "<conjecture>"
CONJECTURE_TAG_END = "</conjecture>"
PROOF_TAG_BEGIN = "<proof>"
PROOF_TAG_END = "</proof>"
FINAL_PROOF_TAG_BEGIN = "<final_proof>"
FINAL_PROOF_TAG_END = "</final_proof>"
CONJECTURE_DIFF_BEGIN = "<conjecture_diff>"
CONJECTURE_DIFF_END = "</conjecture_diff>"
PROOF_DIFF_BEGIN = "<proof_diff>"
PROOF_DIFF_END = "</proof_diff>"


class DiffRefiner(Node):
    """A refiner that can accept either full rewrites or unified diffs.

    Contract:
    - It sends the LLM: shared['messages_for_refiner'] + appended user request.
    - It stores back into shared['messages_for_refiner']:
        (1) an appended *clean* user msg containing the verifier review (no strict formatting rules)
        (2) an appended assistant msg containing FULL updated content wrapped in
            <conjecture> and <proof>/<final_proof> (never stored as diff)
    - It applies diffs (if any) to shared['lemmas'][lemma_id].statement/proof.
    """

    def __init__(self, llm: LLMClient, logger: Logger):
        super(DiffRefiner, self).__init__()
        self.llm = llm
        self.logger = logger
        self.print_to_console = logger.print_to_console_default

    def prep(self, shared):
        # READ ONLY from shared here.
        iteration = shared["verify_refine_round_remaining"]
        if iteration == 0:
            # Pass lemma_id through prep_res so post() can handle quota accounting.
            return AlphaSolveConfig.VERIFIER_EXAUSTED, shared.get("current_lemma_id")

        lemma_id = shared["current_lemma_id"]
        if lemma_id is None:
            self.logger.log_print(
                "event=no_current_lemma step=prep",
                module="diff_refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        lemma = shared["lemmas"][lemma_id]
        ctx_ids = shared.build_reasoning_path(lemma_id, verified_only=True)
        ctx_text = self.__render_context(ctx_ids, shared["lemmas"])

        messages_for_refiner = shared.get("messages_for_refiner") or []
        if not isinstance(messages_for_refiner, list):
            messages_for_refiner = []

        # IMPORTANT: prep() is read-only w.r.t. shared. We therefore prepare the
        # next-round message list *locally* by appending the latest verifier
        # review as a clean role=user message (no strict output-format rules).
        # exec() will use this list as the conversation history.
        clean_review_msg = self.__build_clean_review_message(lemma)
        messages_with_review = list(messages_for_refiner)
        if clean_review_msg:
            last = messages_with_review[-1] if messages_with_review else None
            if not (isinstance(last, dict) and last.get("role") == "user" and last.get("content") == clean_review_msg):
                messages_with_review.append({"role": "user", "content": clean_review_msg})

        strict_instruction_msg = self.__build_strict_instruction_message(
            lemma,
            reasoning_ctx=ctx_text,
        )

        self.logger.log_print(
            f"event=context_built step=prep lemma_id={lemma_id} ctx_size={len(ctx_ids)} remaining={iteration}",
            module="diff_refiner",
            print_to_console=self.print_to_console,
        )

        return AlphaSolveConfig.NORMAL, lemma_id, lemma, messages_with_review, strict_instruction_msg

    def exec(self, prep_res):
        if not prep_res:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            return AlphaSolveConfig.VERIFIER_EXAUSTED, True, None, None

        if len(prep_res) < 5:
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        lemma_id, lemma, messages_with_review, strict_instruction_msg = (
            prep_res[1],
            prep_res[2],
            prep_res[3],
            prep_res[4],
        )
        if not lemma.get("statement") or not lemma.get("proof"):
            return AlphaSolveConfig.EXIT_ON_ERROR, None

        valid, new_lemma, next_messages = self.__refine(
            lemma,
            messages_with_review=messages_with_review,
            strict_instruction_msg=strict_instruction_msg,
        )
        return AlphaSolveConfig.NORMAL, valid, new_lemma, next_messages

    def post(self, shared, prep_res, exec_res):
        # WRITE ONLY to shared here.
        if not prep_res or not exec_res:
            self.logger.log_print(
                "event=illegal_io step=post",
                module="diff_refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        if AlphaSolveConfig.VERIFIER_EXAUSTED == prep_res[0]:
            lemma_id = prep_res[1] if len(prep_res) > 1 else None
            if lemma_id is not None and 0 <= lemma_id < len(shared.get("lemmas", [])):
                lemma = shared["lemmas"][lemma_id]
                lemma["status"] = "rejected"
                if not lemma.get("solver_round_refunded"):
                    lemma["solver_round_refunded"] = True
                    before = shared["solver_round_remaining"]
                    shared["solver_round_remaining"] = min(
                        AlphaSolveConfig.SOLVER_ROUND_NUM,
                        shared["solver_round_remaining"] + 1,
                    )
                    self.logger.log_print(
                        f"event=verify_refine_exhausted_refund step=post lemma_id={lemma_id} solver_round_remaining={before}->{shared['solver_round_remaining']}",
                        module="diff_refiner",
                        print_to_console=self.print_to_console,
                        level="WARNING",
                    )
            return AlphaSolveConfig.EXIT_ON_EXAUSTED

        if len(exec_res) < 4:
            self.logger.log_print(
                "event=illegal_exec_res step=post",
                module="diff_refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        # decrement verify/refine rounds
        shared["verify_refine_round_remaining"] = shared["verify_refine_round_remaining"] - 1

        lemma_id = prep_res[1]
        is_valid, next_lemma, next_messages = exec_res[1], exec_res[2], exec_res[3]

        # Always persist the latest message trace.
        if isinstance(next_messages, list):
            shared["messages_for_refiner"] = next_messages

        if is_valid:
            if next_lemma:
                shared["lemmas"][lemma_id] = next_lemma
                shared["current_lemma_id"] = lemma_id
                self.logger.log_print(
                    f"event=refine_success step=post lemma_id={lemma_id} remaining={shared['verify_refine_round_remaining']}",
                    module="diff_refiner",
                    print_to_console=self.print_to_console,
                )
                return AlphaSolveConfig.REFINE_SUCCESS
            self.logger.log_print(
                f"event=refine_no_output step=post lemma_id={lemma_id}",
                module="diff_refiner",
                print_to_console=self.print_to_console,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

        # refiner considers lemma wrong => route to solver
        shared["lemmas"][lemma_id]["status"] = "rejected"
        self.logger.log_print(
            f"event=lemma_wrong step=post lemma_id={lemma_id} remaining={shared['verify_refine_round_remaining']}",
            module="diff_refiner",
            print_to_console=self.print_to_console,
            level="WARNING",
        )
        return AlphaSolveConfig.CONJECTURE_WRONG

    def __refine(
        self,
        lemma: Dict[str, Any],
        *,
        messages_with_review: List[Dict[str, Any]],
        strict_instruction_msg: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        prior = list(messages_with_review or [])
        messages_to_send = prior + [{"role": "user", "content": strict_instruction_msg}]

        self.logger.log_print(
            "event=llm_messages step=exec\n" + json.dumps(messages_to_send, ensure_ascii=False, indent=2),
            module="diff_refiner",
            print_to_console=self.print_to_console,
        )

        b = time.time()
        answer, cot, _ = self.llm.get_result(messages_to_send)

        self.logger.log_print(
            f"event=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer)} cot_len={len(cot)}",
            module="diff_refiner",
            print_to_console=self.print_to_console,
        )

        valid = INVALID_TAG not in (answer or "")
        new_statement, new_proof, normalized_assistant = self.__parse_and_apply(
            answer or "",
            old_statement=str(lemma.get("statement", "")),
            old_proof=str(lemma.get("proof", "")),
            is_valid=valid,
        )

        # Persist only the clean review user message (already in `prior`) and
        # the assistant's full rewritten content (never as diffs).
        next_messages_for_refiner = prior + [{"role": "assistant", "content": normalized_assistant}]

        if new_statement is None or new_proof is None:
            return valid, None, next_messages_for_refiner

        next_lemma = {
            **lemma,
            "statement": new_statement,
            "proof": new_proof,
            "cot": cot,
            "status": "pending",
        }
        return valid, next_lemma, next_messages_for_refiner

    def __build_clean_review_message(self, lemma: Dict[str, Any]) -> str:
        """Persisted role=user message (keeps verifier review, no strict rules)."""
        review = (lemma.get("review") or "").strip()
        lines: List[str] = []
        lines.append("The verifier reported issues with the current conjecture/proof. Please refine based on the review below.")
        lines.append("")
        lines.append("<review>")
        lines.append(review)
        lines.append("</review>")
        return "\n".join(lines).strip() + "\n"

    def __build_strict_instruction_message(self, lemma: Dict[str, Any], *, reasoning_ctx: Optional[str]) -> str:
        """Non-persisted user message that adds output constraints + current text.

        This is sent to the LLM right after the clean review message.
        """
        statement = (lemma.get("statement") or "").strip()
        proof = (lemma.get("proof") or "").strip()

        lines: List[str] = []
        lines.append("Please produce either:")
        lines.append("(A) Diffs to modify the conjecture/proof: use <conjecture_diff> and/or <proof_diff> with unified diff hunks (starting with @@ / + / - / space).")
        lines.append("or")
        lines.append("(B) A full rewrite: wrap the full text in <conjecture>...</conjecture> and <proof>...</proof> (or <final_proof>...</final_proof>).")
        lines.append("End with \\boxed{true} if you believe the conjecture is true under your revised proof; otherwise end with \\boxed{false}.")
        lines.append("")
        lines.append("CURRENT conjecture:")
        lines.append(CONJECTURE_TAG_BEGIN)
        lines.append(statement)
        lines.append(CONJECTURE_TAG_END)
        lines.append("")
        lines.append("CURRENT proof:")
        lines.append(PROOF_TAG_BEGIN)
        lines.append(proof)
        lines.append(PROOF_TAG_END)
        lines.append("")
        if reasoning_ctx:
            lines.append(str(reasoning_ctx).strip())
        return "\n".join(lines).strip() + "\n"

    def __extract_tag(self, text: str, begin: str, end: str) -> Optional[str]:
        if text is None:
            return None
        b = text.find(begin)
        e = text.find(end)
        if b < 0 or e < 0 or b + len(begin) > e:
            return None
        b += len(begin)
        return text[b:e]

    def __apply_unified_diff(self, original: str, diff_text: str) -> Optional[str]:
        """Apply unified diff hunks to original.

        Returns patched text, or None if parsing/verification fails.
        """
        if not diff_text or "@@" not in diff_text:
            return None

        orig_lines = original.splitlines(True)
        diff_lines = diff_text.splitlines(True)

        out: List[str] = []
        i = 0
        hunk_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

        idx = 0
        try:
            while idx < len(diff_lines):
                line = diff_lines[idx]
                if line.startswith("---") or line.startswith("+++") or line.startswith("diff "):
                    idx += 1
                    continue

                m = hunk_re.match(line)
                if not m:
                    idx += 1
                    continue

                old_start = int(m.group(1))
                target = max(0, old_start - 1)
                if target < i:
                    return None
                out.extend(orig_lines[i:target])
                i = target
                idx += 1

                while idx < len(diff_lines) and not diff_lines[idx].startswith("@@"):
                    hl = diff_lines[idx]
                    if hl.startswith("\\ No newline at end of file"):
                        idx += 1
                        continue

                    if not hl:
                        idx += 1
                        continue

                    tag = hl[0]
                    payload = hl[1:]
                    if tag == " ":
                        if i >= len(orig_lines):
                            return None
                        if orig_lines[i].rstrip("\n") != payload.rstrip("\n"):
                            return None
                        out.append(orig_lines[i])
                        i += 1
                    elif tag == "-":
                        if i >= len(orig_lines):
                            return None
                        if orig_lines[i].rstrip("\n") != payload.rstrip("\n"):
                            return None
                        i += 1
                    elif tag == "+":
                        if payload and not payload.endswith("\n"):
                            payload = payload + "\n"
                        out.append(payload)
                    else:
                        return None
                    idx += 1

            out.extend(orig_lines[i:])
            return "".join(out)
        except Exception:
            return None

    def __parse_and_apply(
        self,
        model_output: str,
        *,
        old_statement: str,
        old_proof: str,
        is_valid: bool,
    ) -> Tuple[Optional[str], Optional[str], str]:
        """Parse model output and return updated (statement, proof, normalized_message)."""

        # Prefer full rewrites.
        statement_full = self.__extract_tag(model_output, CONJECTURE_TAG_BEGIN, CONJECTURE_TAG_END)
        proof_full = self.__extract_tag(model_output, PROOF_TAG_BEGIN, PROOF_TAG_END)
        final_proof_full = self.__extract_tag(model_output, FINAL_PROOF_TAG_BEGIN, FINAL_PROOF_TAG_END)

        statement_diff = self.__extract_tag(model_output, CONJECTURE_DIFF_BEGIN, CONJECTURE_DIFF_END)
        proof_diff = self.__extract_tag(model_output, PROOF_DIFF_BEGIN, PROOF_DIFF_END)

        new_statement: Optional[str] = None
        new_proof: Optional[str] = None
        use_final = False

        # Conjecture
        if statement_full is not None:
            new_statement = statement_full
        elif statement_diff is not None:
            patched = self.__apply_unified_diff(old_statement, statement_diff)
            if patched is None:
                self.logger.log_print(
                    "event=diff_apply_failed kind=conjecture",
                    module="diff_refiner",
                    level="ERROR",
                )
                new_statement = None
            else:
                new_statement = patched
        else:
            # No conjecture edits provided.
            new_statement = old_statement

        # Proof
        if final_proof_full is not None:
            use_final = True
            new_proof = final_proof_full
        elif proof_full is not None:
            new_proof = proof_full
        elif proof_diff is not None:
            patched = self.__apply_unified_diff(old_proof, proof_diff)
            if patched is None:
                self.logger.log_print(
                    "event=diff_apply_failed kind=proof",
                    module="diff_refiner",
                    level="ERROR",
                )
                new_proof = None
            else:
                new_proof = patched
        else:
            new_proof = None

        # Always store a normalized full version (no diff) in message history.
        if use_final:
            normalized = (
                f"{CONJECTURE_TAG_BEGIN}\n{(new_statement or old_statement).strip()}\n{CONJECTURE_TAG_END}\n\n"
                f"{FINAL_PROOF_TAG_BEGIN}\n{(new_proof or '').strip()}\n{FINAL_PROOF_TAG_END}\n\n"
                + ("\\boxed{true}" if is_valid else "\\boxed{false}")
            )
        else:
            normalized = (
                f"{CONJECTURE_TAG_BEGIN}\n{(new_statement or old_statement).strip()}\n{CONJECTURE_TAG_END}\n\n"
                f"{PROOF_TAG_BEGIN}\n{(new_proof or '').strip()}\n{PROOF_TAG_END}\n\n"
                + ("\\boxed{true}" if is_valid else "\\boxed{false}")
            )

        if new_statement is None or new_proof is None:
            return None, None, normalized
        return new_statement, new_proof, normalized

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


def create_diff_refiner_agent(logger: Logger):
    llm = LLMClient(AlphaSolveConfig.REFINER_CONFIG, logger=logger)
    return DiffRefiner(llm, logger=logger)

