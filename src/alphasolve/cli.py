from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from alphasolve.agents.team import FilesystemAlphaSolve
from alphasolve.agents.team.demo import make_demo_client_factory
from alphasolve.config.agent_config import AlphaSolveConfig
from alphasolve.runtime.wolfram_probe import check_wolfram_kernel
from alphasolve.utils.log_session import LogSession
from alphasolve.utils.utils import load_prompt_from_file
from alphasolve.workflow import AlphaSolve


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
        "--lemmaworkers",
        type=int,
        default=None,
        help="Maximum number of concurrent lemmaworkers for the filesystem workflow",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to an agent suite YAML file or a directory containing agents.yaml",
    )
    parser.add_argument(
        "--max_verify_rounds",
        type=int,
        default=None,
        help="Maximum verifier/reviser rounds per lemmaworker (default: from agents.yaml settings, fallback 2)",
    )
    parser.add_argument(
        "--subagent_max_depth",
        type=int,
        default=None,
        help="Maximum recursive depth for configured subagents (default: from agents.yaml settings, fallback 2)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a deterministic local demo without calling an LLM API",
    )
    parser.add_argument(
        "--no_wolfram_prime",
        action="store_true",
        help="Skip the startup Wolfram kernel probe for the filesystem workflow",
    )
    parser.add_argument(
        "--no_dashboard",
        action="store_true",
        help="Disable the live terminal dashboard for the filesystem workflow",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Run the previous schema-based AlphaSolve workflow",
    )
    parser.add_argument(
        "--tool_executor_size",
        type=int,
        default=default_max_worker_num,
        help="Number of Python execution worker processes for code execution tools",
    )
    parser.add_argument(
        "--init_from_previous",
        type=lambda x: x.lower() in ("true", "1", "yes"),
        default=True,
        help="Whether to start from the previous version of lemma pool (default: True)",
    )

    args = parser.parse_args()

    if not args.legacy:
        from alphasolve.agents.general import load_agent_suite_config
        from alphasolve.config.agent_config import PACKAGE_ROOT
        config_path = Path(args.config).resolve() if args.config else Path(PACKAGE_ROOT) / "config"
        suite_settings = load_agent_suite_config(config_path).settings
        max_verify_rounds = args.max_verify_rounds if args.max_verify_rounds is not None else int(suite_settings.get("max_verify_rounds", 2))
        subagent_max_depth = args.subagent_max_depth if args.subagent_max_depth is not None else int(suite_settings.get("subagent_max_depth", 2))
        result = FilesystemAlphaSolve(
            project_dir=Path.cwd(),
            problem=args.problem,
            hint=args.hint,
            config_path=args.config,
            max_workers=args.lemmaworkers or default_max_worker_num,
            max_verify_rounds=max_verify_rounds,
            subagent_max_depth=subagent_max_depth,
            client_factory=make_demo_client_factory() if args.demo else None,
            prime_wolfram=not args.no_wolfram_prime,
            print_to_console=not args.no_dashboard,
            tool_executor_size=args.tool_executor_size,
        ).run()
        print(result.final_answer or "")
        return

    problem = load_prompt_from_file(args.problem)

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

        logger.log_section("Final Solution", width=60)
        logger.log_print(solution or "")

        output_file = "solution.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("# Problem\n\n")
            f.write(f"```\n{problem}\n```\n\n")
            f.write("# Solution\n\n")
            f.write(solution or "")

        logger.log_print(f"event=solution_saved path={output_file}", module="startup", level="SUCCESS")
    finally:
        alpha.do_close()


if __name__ == "__main__":
    main()
