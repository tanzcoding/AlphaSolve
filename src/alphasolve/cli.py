from __future__ import annotations

import os
import sys
import argparse

# Ensure UTF-8 on Windows so Rich spinners and Unicode symbols render correctly.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from alphasolve.workflow import AlphaSolve
from alphasolve.utils.utils import load_prompt_from_file
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.utils.log_session import LogSession
from alphasolve.runtime.wolfram_probe import check_wolfram_kernel


def main():
    default_max_worker_num = max(1, (os.cpu_count() or 1) - 2)

    parser = argparse.ArgumentParser(description="Run AlphaSolve.")
    parser.add_argument(
        "--problem",
        type=str,
        default="problem.md",
        help="Path to the problem markdown file (default: problem.md in current directory)",
    )
    parser.add_argument(
        "--hint",
        type=str,
        default=None,
        help="Path to an optional hint markdown file",
    )
    parser.add_argument(
        "--tool_executor_size",
        type=int,
        default=default_max_worker_num,
        help="Number of Python execution worker processes",
    )
    parser.add_argument(
        "--init_from_previous",
        type=lambda x: x.lower() in ("true", "1", "yes"),
        default=True,
        help="Whether to start from the previous version of lemma pool (default: True)",
    )

    args = parser.parse_args()

    # 读取问题文件
    problem = load_prompt_from_file(args.problem)

    # 读取可选 hint
    hint = None
    if args.hint and os.path.exists(args.hint):
        hint = load_prompt_from_file(args.hint)

    log_session = LogSession(
        run_root=AlphaSolveConfig.LOG_PATH,
        progress_path=AlphaSolveConfig.PROGRESS_PATH,
    )
    logger = log_session.main_logger(print_to_console=True)

    wolfram_check = check_wolfram_kernel()
    AlphaSolveConfig.configure_wolfram_availability(
        wolfram_check.available,
        wolfram_check.reason,
    )
    if wolfram_check.available:
        logger.log_print(
            f"event=wolfram_kernel_available reason={wolfram_check.reason} kernel_path={wolfram_check.kernel_path}",
            module="startup",
        )
    else:
        logger.log_print(
            f"event=wolfram_kernel_unavailable reason={wolfram_check.reason}; run_wolfram tool disabled",
            module="startup",
            level="WARNING",
        )

    alpha = AlphaSolve(
        problem=problem,
        tool_executor_size=args.tool_executor_size,
        log_session=log_session,
        logger=logger,
        init_from_previous=args.init_from_previous,
    )

    try:
        solution = alpha.do_research()

        print("Final Solution:")
        print(solution)

        output_file = "solution.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("# Problem\n\n")
            f.write(f"```\n{problem}\n```\n\n")
            f.write("# Solution\n\n")
            f.write(solution or "")

        print(f"\nSolution has been saved to: {output_file}")
    finally:
        alpha.do_close()


if __name__ == "__main__":
    main()
