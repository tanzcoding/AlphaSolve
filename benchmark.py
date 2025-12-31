import os
import time
import json
import argparse
import threading
import multiprocessing as mp

from datetime import datetime

from workflow import AlphaSolve
from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import log_print

from concurrent.futures import ProcessPoolExecutor, as_completed


EVAL_TAG_CORRECT = "[[VERDICT:CORRECT]]"
EVAL_TAG_INCORRECT = "[[VERDICT:INCORRECT]]"

CONSOLE_LOCK = threading.Lock()

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
    client = LLMClient(AlphaSolveConfig.VERIFIER_CONFIG, print_to_console=False)
    prompt = build_eval_prompt(problem, gold, pred)
    messages = [{"role": "user", "content": prompt}]
    answer, _cot = client.get_result(messages)
    text = (answer or "").strip()
    is_correct = EVAL_TAG_CORRECT in text and EVAL_TAG_INCORRECT not in text
    return is_correct, text

def call_alpha_solve(print_to_console: bool):
    alpha = AlphaSolve(print_to_console)
    solution_text = alpha.do_research()
    return solution_text


def run_once(problem_text: str, gold_text: str, console_lock):
    """
    运行一次 AlphaSolve 流程
    
    注意：print_to_console 控制是否打印到控制台，但为了避免阻塞其他线程，
    我们不会在整个执行过程中持有控制台锁。而是在需要打印时临时获取。
    这样可以避免一个线程在执行耗时的工具调用时阻止其他线程的执行。
    """
    t0 = time.time()
    solution_text = None
    error = None

    # 尝试获取打印权限（非阻塞）
    # NOTE: benchmark 改为多进程后，必须使用跨进程共享的 lock（由 main() 创建并传入）。
    # 这里兼容 multiprocessing.Lock / Manager().Lock 两种接口。
    try:
        has_print_permission = console_lock.acquire(False)
    except TypeError:
        has_print_permission = console_lock.acquire(blocking=False)

    if has_print_permission:
        log_print('[benchmark-worker] begin', print_to_console=has_print_permission)

    try:
        if has_print_permission:
            log_print(f"[benchmark-worker] print_to_console={has_print_permission} ...", print_to_console=has_print_permission)

        # 执行 AlphaSolve，传入打印权限
        # 注意：由于工具调用可能耗时很长，我们在 AlphaSolve 内部不会一直持有锁
        solution_text = call_alpha_solve(has_print_permission)
    except Exception as e:
        error = f"AlphaSolve error: {e}"
    finally:
        # 释放打印权限
        if has_print_permission:
            try:
                console_lock.release()
            except Exception:
                pass

    if has_print_permission:
        log_print('[benchmark-worker] end, evaluate', print_to_console=has_print_permission)

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

    if has_print_permission:
        log_print('[benchmark-worker] end', print_to_console=has_print_permission)

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

    log_print(f"[benchmark] starting {args.runs} runs ...", print_to_console=True)

    # 多进程并行：避免多线程共享同一进程带来的 GIL/CPU 抢占与 stdout 交错。
    max_worker_num = max(1, (os.cpu_count() or 1) - 2)
    futures = [ ]

    log_print(f"[benchmark] starting {max_worker_num} workers for parallel ...", print_to_console=True)

    # 跨进程共享的 console lock：确保同一时刻只有一个 AlphaSolve 能打印。
    # 使用 Manager().Lock()，可在 Windows spawn 模式下安全传递给子进程。
    manager = mp.Manager()
    console_lock = manager.Lock()

    executor = ProcessPoolExecutor(max_workers=max_worker_num, mp_context=mp.get_context("spawn"))


    for i in range(1, args.runs + 1):
        log_print(f"[benchmark] run {i}/{args.runs}", print_to_console=True)

        future = executor.submit(run_once, problem_text, gold_text, console_lock)
        futures.append(future)

        if args.sleep > 0:
            time.sleep(args.sleep)

    finished = 0
    for fut in as_completed(futures):
        try:
            data = fut.result()
            results.append(data)
            finished += 1
            log_print(f"[benchmark] finished {finished}/{len(futures)}", print_to_console=True)
            if data.get("correct"):
                correct_count += 1
        except Exception as exc:
            finished += 1
            results.append({
                "elapsed_sec": None,
                "alpha_answer": "",
                "eval_decision": "EXCEPTION",
                "correct": False,
                "evaluator_raw": str(exc),
            })
            log_print(f"[benchmark] exception from worker: {exc}", print_to_console=True)

    executor.shutdown(wait=True)
    try:
        manager.shutdown()
    except Exception:
        pass


    accuracy = correct_count / max(1, args.runs)
    summary = {
        "runs": args.runs,
        "correct": correct_count,
        "accuracy": accuracy,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    log_print("[benchmark] summary:", print_to_console=True)
    log_print(json.dumps(summary, ensure_ascii=False, indent=2), print_to_console=True)

    out_path = args.out or f"benchmark_results_{int(time.time())}.json"
    payload = {"summary": summary, "results": results}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log_print(f"[benchmark] detailed results saved to: {out_path}", print_to_console=True)


if __name__ == "__main__":
    main()

