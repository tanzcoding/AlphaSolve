from __future__ import annotations

import argparse
import ctypes
import os
import signal
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from alphasolve.solver import AlphaSolve
from alphasolve.solver.policy import SolverPolicy

_app: AlphaSolve | None = None
_interrupt_count = 0

# ---------------------------------------------------------------------------
# Windows console control handler (bypasses Python's signal module entirely
# because signal.signal(SIGINT, ...) can silently stop working when the main
# thread is blocked in Winsock I/O and another thread is doing heavy console
# output).
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    _kernel32 = ctypes.windll.kernel32
    _STD_ERROR_HANDLE = ctypes.c_ulong(0xFFFFFFF4)  # STD_ERROR_HANDLE

    # Console control handler callback type
    _PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_ulong)

    def _win_write_stderr(text: bytes) -> None:
        """Write raw bytes to stderr via the Windows console API.
        This bypasses Python's I/O stack and works from any thread."""
        written = ctypes.c_ulong(0)
        handle = _kernel32.GetStdHandle(_STD_ERROR_HANDLE)
        _kernel32.WriteFile(
            handle,
            text,
            ctypes.c_ulong(len(text)),
            ctypes.byref(written),
            None,
        )

    @_PHANDLER_ROUTINE
    def _win_ctrl_handler(ctrl_type: int) -> int:
        """Windows console control handler — called in a dedicated thread."""
        if ctrl_type != 0:  # CTRL_C_EVENT
            return 0  # FALSE — pass to next handler
        global _interrupt_count
        _interrupt_count += 1
        if _interrupt_count == 1:
            if _app is not None:
                _app.cancel()  # sets threading.Event (always non-blocking)
            _win_write_stderr(
                b"\r\nInterrupted -- finishing current work..."
                b" (press Ctrl+C again to force exit)\r\n"
            )
            return 1  # TRUE — event handled
        # 第二次 Ctrl+C 会绕过 Python 清理流程，必须直接恢复光标和主屏幕缓冲区。
        _win_write_stderr(b"\x1b[?25h\x1b[?1049l\r\nForce quit.\r\n")
        os._exit(130)

    def _install_console_handler() -> None:
        """Register our handler.  Returns True on success."""
        ok = _kernel32.SetConsoleCtrlHandler(_win_ctrl_handler, 1)
        if not ok:
            _win_write_stderr(
                b"WARNING: SetConsoleCtrlHandler failed (error %d)\r\n"
                % _kernel32.GetLastError()
            )

else:  # Unix
    def _on_interrupt(_signum: int, _frame: object) -> None:
        global _interrupt_count
        _interrupt_count += 1
        if _interrupt_count == 1:
            if _app is not None:
                _app.cancel()
            print(
                "\nInterrupted — finishing current work…"
                " (press Ctrl+C again to force exit)",
                file=sys.stderr,
            )
            return
        # Second Ctrl+C
        sys.stderr.write("\nForce quit.\n")
        sys.stderr.flush()
        os._exit(130)


def _apply_env_sources(
    *,
    cwd_env_path: Path,
    user_env_path: Path,
    env_overrides: list[str],
) -> None:
    """Populate os.environ from .env files and --env CLI flags.

    Precedence (highest first):
      1. --env KEY=VAL flags (always win)
      2. Existing os.environ (shell exports)
      3. cwd .env (project-local)
      4. user .env (~/.alphasolve/.env or $ALPHASOLVE_CONFIG_DIR/.env)

    .env files never overwrite already-set env vars. --env flags always do.
    Raises ValueError on malformed --env arguments.
    """
    from dotenv import load_dotenv

    # Load highest-priority .env first; override=False on subsequent loads
    # means user .env only fills variables not set by cwd .env (or shell).
    if cwd_env_path.is_file():
        load_dotenv(cwd_env_path, override=False)
    if user_env_path.is_file():
        load_dotenv(user_env_path, override=False)

    # --env always wins, applied after .env loading
    for entry in env_overrides:
        if "=" not in entry:
            raise ValueError(f"--env expects KEY=VAL format, got: {entry!r}")
        key, _, value = entry.partition("=")
        if not key:
            raise ValueError(f"--env key must not be empty: {entry!r}")
        os.environ[key] = value


def main() -> None:
    global _app

    if sys.platform == "win32":
        _install_console_handler()
    else:
        signal.signal(signal.SIGINT, _on_interrupt)

    parser = argparse.ArgumentParser(description="Run AlphaSolve.")
    parser.add_argument("--problem", type=str, default="problem.md",
                        help="Path to the problem markdown file (default: problem.md)")
    parser.add_argument("--hint", type=str, default=None,
                        help="Path to an optional hint markdown file")
    parser.add_argument("--workers", type=int, default=None,
                        help="Maximum number of concurrent workers (default: from agents.yaml)")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to an agent suite YAML file or directory containing agents.yaml")
    parser.add_argument("--max_verify_rounds", type=int, default=None,
                        help="Maximum verifier/reviser rounds per worker (default: from agents.yaml)")
    parser.add_argument("--verifier_scaling_factor", type=int, default=None,
                        help="Independent verifier attempts per verifier round (default: from agents.yaml)")
    parser.add_argument("--subagent_max_depth", type=int, default=None,
                        help="Maximum recursive depth for subagents (default: from agents.yaml)")
    parser.add_argument("--debug", action="store_true",
                        help="Produce detailed solver trace logs under logs/. With --agent -p, print intermediate reasoning and tool events to stderr.")
    parser.add_argument("--agent", action="store_true",
                        help="Chat with a Codex agent using AlphaSolve tools")
    parser.add_argument("--profile", choices=["generic", "orchestrator", "generator"], default="generic",
                        help="Agent profile to use with --agent (default: generic)")
    parser.add_argument("--worker-dir", default=None,
                        help="Generator worker directory relative to the current workspace snapshot")
    parser.add_argument("--show-tools", action="store_true",
                        help="Print the selected agent's effective tool descriptions and schemas without calling a model")
    parser.add_argument("-p", "--print", dest="agent_prompt", metavar="PROMPT",
                        help="Run --agent once with PROMPT and print the final answer")
    parser.add_argument("--no_wolfram_prime", action="store_true",
                        help="Skip the startup Wolfram kernel probe")
    parser.add_argument("--no_dashboard", action="store_true",
                        help="Disable the live terminal dashboard")
    parser.add_argument("--tool_executor_size", type=int, default=None,
                        help="Maximum concurrent Python executions (default: from agents.yaml)")
    parser.add_argument("--max_orchestrator_restarts", type=int, default=None,
                        help="Maximum Ralph-loop orchestrator restarts (default: from agents.yaml or 5)")
    parser.add_argument("--list-tiers", action="store_true",
                        help="List available tiers and exit")
    parser.add_argument("--list-presets", action="store_true",
                        help="List available presets and exit")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VAL",
                        help="Set an environment variable for this run (repeatable). "
                             "Overrides shell env and .env files for that key.")

    args = parser.parse_args()
    if args.agent_prompt is not None and not args.agent:
        parser.error("-p/--print can only be used with --agent")
    if args.profile != "generic" and not args.agent:
        parser.error("--profile can only be used with --agent")
    if args.show_tools and not args.agent:
        parser.error("--show-tools can only be used with --agent")
    if args.worker_dir is not None and (not args.agent or args.profile != "generator"):
        parser.error("--worker-dir requires --agent --profile generator")

    from alphasolve.agent import AgentRunError, load_agent_suite

    PACKAGE_ROOT = Path(__file__).resolve().parent
    from alphasolve.llm import load_presets, load_tier_mapping, make_client_factory

    user_dir_env = os.getenv("ALPHASOLVE_CONFIG_DIR")
    user_dir = Path(user_dir_env) if user_dir_env else Path.home() / ".alphasolve"
    try:
        _apply_env_sources(
            cwd_env_path=Path.cwd() / ".env",
            user_env_path=user_dir / ".env",
            env_overrides=args.env,
        )
    except ValueError as exc:
        parser.error(str(exc))

    presets_path = PACKAGE_ROOT / "config" / "presets.yaml"
    tiers_path = PACKAGE_ROOT / "config" / "tiers.yaml"
    user_presets = user_dir / "presets.yaml" if (user_dir / "presets.yaml").is_file() else None
    user_tiers = user_dir / "tiers.yaml" if (user_dir / "tiers.yaml").is_file() else None

    if args.list_presets:
        presets = load_presets(repo_path=presets_path, user_path=user_presets)
        for name in sorted(presets):
            p = presets[name]
            effort = p.reasoning_effort or "(Codex default)"
            print(
                f"  {name:<28} {p.provider:<22} "
                f"{p.model or '(Codex default)'}  reasoning={effort}"
            )
        return

    if args.list_tiers:
        import yaml as _yaml
        merged: dict[str, str] = {}
        for path in (tiers_path, user_tiers):
            if path is None or not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            data.pop("default", None)
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    merged[k] = v
        for tier in sorted(merged):
            print(f"  {tier} -> {merged[tier]}")
        return

    config_path = Path(args.config).resolve() if args.config else PACKAGE_ROOT / "solver" / "config"
    suite = load_agent_suite(config_path)

    tier_mapping = load_tier_mapping(repo_path=tiers_path, user_path=user_tiers)
    presets = load_presets(repo_path=presets_path, user_path=user_presets)
    client_factory = make_client_factory(tier_mapping, presets)

    try:
        policy = SolverPolicy.from_settings(suite.settings).with_overrides(
            max_workers=args.workers,
            max_verify_rounds=args.max_verify_rounds,
            verifier_scaling_factor=args.verifier_scaling_factor,
            subagent_max_depth=args.subagent_max_depth,
            tool_executor_size=args.tool_executor_size,
            max_orchestrator_restarts=args.max_orchestrator_restarts,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.agent:
        from alphasolve.agent.ui.cli_app import AgentApp, make_print_debug_event_sink

        if args.profile == "generic":
            _app = AgentApp(
                project_dir=Path.cwd(),
                client_factory=client_factory,
            )
        elif args.profile == "generator":
            from alphasolve.solver.role_agent_app import GeneratorAgentApp

            _app = GeneratorAgentApp(
                project_dir=Path.cwd(),
                suite=suite,
                client_factory=client_factory,
                policy=policy,
                worker_dir=args.worker_dir or "unverified_propositions/agent-test",
            )
        else:
            from alphasolve.solver.orchestrator_agent_app import OrchestratorAgentApp

            _app = OrchestratorAgentApp(
                project_dir=Path.cwd(),
                suite=suite,
                client_factory=client_factory,
                policy=policy,
            )
        try:
            if args.show_tools:
                import json
                from dataclasses import asdict

                print(json.dumps([asdict(tool) for tool in _app.tool_defs()], ensure_ascii=False, indent=2))
            elif args.agent_prompt is not None:
                if args.debug:
                    from rich.console import Console

                    debug_sink = make_print_debug_event_sink(Console(stderr=True))
                else:
                    debug_sink = None
                result = _app.run_once(args.agent_prompt, event_sink=debug_sink)
                print(result.final_answer or "")
            else:
                _app.run()
        except AgentRunError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            sys.exit(130)
        finally:
            close = getattr(_app, "close", None)
            if callable(close):
                close()
        return

    try:
        _app = AlphaSolve(
            project_dir=Path.cwd(),
            problem=args.problem,
            hint=args.hint,
            config_path=config_path,
            policy=policy,
            client_factory=client_factory,
            prime_wolfram=not args.no_wolfram_prime,
            print_to_console=not args.no_dashboard,
            debug=args.debug,
        )
        result = _app.run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

    print(result.final_answer or "")


if __name__ == "__main__":
    main()
