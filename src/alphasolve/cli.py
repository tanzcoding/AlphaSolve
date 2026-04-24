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

from alphasolve.agents.team import AlphaSolve
from alphasolve.agents.team.demo import make_demo_client_factory


def main():
    default_max_workers = max(1, (os.cpu_count() or 1) - 2)

    parser = argparse.ArgumentParser(description="Run AlphaSolve.")
    parser.add_argument("--problem", type=str, default="problem.md",
                        help="Path to the problem markdown file (default: problem.md)")
    parser.add_argument("--hint", type=str, default=None,
                        help="Path to an optional hint markdown file")
    parser.add_argument("--lemmaworkers", type=int, default=None,
                        help="Maximum number of concurrent lemma workers")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to an agent suite YAML file or directory containing agents.yaml")
    parser.add_argument("--max_verify_rounds", type=int, default=None,
                        help="Maximum verifier/reviser rounds per lemma worker (default: from agents.yaml)")
    parser.add_argument("--subagent_max_depth", type=int, default=None,
                        help="Maximum recursive depth for subagents (default: from agents.yaml)")
    parser.add_argument("--demo", action="store_true",
                        help="Run a deterministic local demo without calling an LLM API")
    parser.add_argument("--no_wolfram_prime", action="store_true",
                        help="Skip the startup Wolfram kernel probe")
    parser.add_argument("--no_dashboard", action="store_true",
                        help="Disable the live terminal dashboard")
    parser.add_argument("--tool_executor_size", type=int, default=default_max_workers,
                        help="Number of Python execution worker processes")

    args = parser.parse_args()

    from alphasolve.agents.general import load_agent_suite_config
    from alphasolve.config.agent_config import PACKAGE_ROOT
    config_path = Path(args.config).resolve() if args.config else Path(PACKAGE_ROOT) / "config"
    suite_settings = load_agent_suite_config(config_path).settings
    max_verify_rounds = args.max_verify_rounds if args.max_verify_rounds is not None else int(suite_settings.get("max_verify_rounds", 2))
    subagent_max_depth = args.subagent_max_depth if args.subagent_max_depth is not None else int(suite_settings.get("subagent_max_depth", 2))

    try:
        result = AlphaSolve(
            project_dir=Path.cwd(),
            problem=args.problem,
            hint=args.hint,
            config_path=args.config,
            max_workers=args.lemmaworkers or default_max_workers,
            max_verify_rounds=max_verify_rounds,
            subagent_max_depth=subagent_max_depth,
            client_factory=make_demo_client_factory() if args.demo else None,
            prime_wolfram=not args.no_wolfram_prime,
            print_to_console=not args.no_dashboard,
            tool_executor_size=args.tool_executor_size,
        ).run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    print(result.final_answer or "")


if __name__ == "__main__":
    main()
