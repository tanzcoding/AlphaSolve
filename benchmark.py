import os
import time
import json
import argparse
from datetime import datetime

from workflow import AlphaSolve
from config.agent_config import AlphaSolveConfig, VERIFIER_CONFIG
from llms.utils import LLMClient


EVAL_TAG_CORRECT = "[[VERDICT:CORRECT]]"
EVAL_TAG_INCORRECT = "[[VERDICT:INCORRECT]]"


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def build_eval_prompt(problem: str, gold: str, pred: str) -> str:
    return f"""
You are a strict evaluator for mathematical problem solving.

Task:
- Compare the Candidate Answer from AlphaSolve with the Gold Standard Answer.
- Decide if the Candidate is mathematically equivalent to the Gold.

Important rules:
- Consider mathematical equivalence, not formatting.
- If sets/expressions are equivalent up to re-indexing or simple algebra, it is CORRECT.
- If the candidate is empty, undefined, or clearly mismatched, it is INCORRECT.
- Output ONLY one of the following tokens as the last line: {EVAL_TAG_CORRECT} or {EVAL_TAG_INCORRECT}.
- Do not add any extra text after the token.

Problem:
{problem}

Gold Standard Answer:
{gold}

Candidate Answer (AlphaSolve):
{pred}

Now decide and output exactly one final token on the last line.
""".strip()


def evaluate_with_llm(problem: str, gold: str, pred: str) -> tuple[bool, str]:
    """Use an LLM to judge if pred matches gold. Returns (is_correct, raw_reply)."""
    client = LLMClient(VERIFIER_CONFIG)
    prompt = build_eval_prompt(problem, gold, pred)
    messages = [{"role": "user", "content": prompt}]
    answer, _cot = client.get_result(messages, print_to_console=False)
    text = (answer or "").strip()
    is_correct = EVAL_TAG_CORRECT in text and EVAL_TAG_INCORRECT not in text
    return is_correct, text


def run_once(problem_text: str, gold_text: str):
    t0 = time.time()
    solution_text = None
    error = None

    try:
        alpha = AlphaSolve()
        # do_research may return None if summarizer is not implemented
        solution_text = alpha.do_research()
    except Exception as e:
        error = f"AlphaSolve error: {e}"

    # Fallbacks: treat None/empty as incorrect
    if not solution_text:
        elapsed = time.time() - t0
        return {
            "elapsed_sec": elapsed,
            "alpha_answer": solution_text or "",
            "eval_decision": "NO_SOLUTION",
            "correct": False,
            "evaluator_raw": error or "",
        }

    # Evaluate with LLM
    ok, raw = evaluate_with_llm(problem_text, gold_text, solution_text)
    elapsed = time.time() - t0
    return {
        "elapsed_sec": elapsed,
        "alpha_answer": solution_text,
        "eval_decision": "CORRECT" if ok else "INCORRECT",
        "correct": bool(ok),
        "evaluator_raw": raw,
    }


def main():
    parser = argparse.ArgumentParser(description="Run AlphaSolve benchmark.")
    parser.add_argument("-n", "--runs", type=int, default=10, help="Number of runs (default: 10)")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between runs (default: 0)")
    parser.add_argument("--out", type=str, default=None, help="Output JSON file (default: auto name)")
    args = parser.parse_args()

    problem_path = AlphaSolveConfig.PROBLEM_PATH
    gold_path = AlphaSolveConfig.STANDARD_SOLUTION_PATH

    problem_text = _read_text(problem_path)
    gold_text = _read_text(gold_path)

    results = []
    correct_count = 0

    print(f"[benchmark] starting {args.runs} runs ...")
    for i in range(1, args.runs + 1):
        print(f"[benchmark] run {i}/{args.runs}")
        res = run_once(problem_text, gold_text)
        results.append(res)
        if res.get("correct"):
            correct_count += 1
        if args.sleep > 0:
            time.sleep(args.sleep)

    accuracy = correct_count / max(1, args.runs)
    summary = {
        "runs": args.runs,
        "correct": correct_count,
        "accuracy": accuracy,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    print("[benchmark] summary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    out_path = args.out or f"benchmark_results_{int(time.time())}.json"
    payload = {"summary": summary, "results": results}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[benchmark] detailed results saved to: {out_path}")


if __name__ == "__main__":
    main()

