from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Any, Callable

from alphasolve.agents.general import AgentRunError, GeneralAgentConfig, OpenAIChatClient, load_agent_suite_config
from alphasolve.config.agent_config import AlphaSolveConfig, PACKAGE_ROOT
from alphasolve.execution import ExecutionGateway
from alphasolve.runtime.wolfram_probe import check_wolfram_kernel
from alphasolve.utils.rich_renderer import LemmaTeamRenderer

from .knowledge_digest import KnowledgeDigestQueue, init_knowledge_base
from .orchestrator import Orchestrator, OrchestratorRunResult, verified_count
from .project import ProjectLayout
from .tools import ClientFactory


class AlphaSolve:
    def __init__(
        self,
        *,
        project_dir: str | Path,
        problem: str | Path = "problem.md",
        hint: str | Path | None = None,
        config_path: str | Path | None = None,
        max_workers: int = 2,
        max_verify_rounds: int = 2,
        verifier_scaling_factor: int | None = None,
        subagent_max_depth: int = 2,
        client_factory: ClientFactory | None = None,
        prime_wolfram: bool = True,
        print_to_console: bool = True,
        tool_executor_size: int = 2,
        execution_gateway: ExecutionGateway | None = None,
        max_orchestrator_restarts: int = 5,
    ) -> None:
        self.layout = ProjectLayout.create(project_dir, problem=problem, hint=hint)
        self.config_path = Path(config_path).resolve() if config_path else Path(PACKAGE_ROOT) / "config"
        self.max_workers = max(1, int(max_workers))
        self.max_verify_rounds = max(1, int(max_verify_rounds))
        self.verifier_scaling_factor_override = verifier_scaling_factor
        self.subagent_max_depth = max(0, int(subagent_max_depth))
        self.client_factory_override = client_factory
        self.prime_wolfram = prime_wolfram
        self.print_to_console = print_to_console
        self.tool_executor_size = max(1, int(tool_executor_size))
        self.execution_gateway_override = execution_gateway
        self.max_orchestrator_restarts = max(1, int(max_orchestrator_restarts))

    def run(self) -> OrchestratorRunResult:
        renderer = LemmaTeamRenderer(screen=False) if self.print_to_console else None
        execution_gateway: ExecutionGateway | None = None
        owns_gateway = self.execution_gateway_override is None
        digest_queue: KnowledgeDigestQueue | None = None
        if renderer is not None:
            renderer.start()
            renderer.update_orchestrator_phase("startup", status="running")
        try:
            self.layout.ensure()
            startup: dict[str, Any] = {
                "project_root": str(self.layout.project_root),
                "workspace": str(self.layout.workspace_dir),
                "config_path": str(self.config_path),
            }
            if renderer is not None:
                renderer.log(None, f"workspace: {self.layout.workspace_dir}", module="startup")
                renderer.update_pool(verified_count=verified_count(self.layout.verified_dir))
            if self.prime_wolfram:
                if renderer is not None:
                    renderer.update_orchestrator_phase("wolfram probe", status="running")
                probe = check_wolfram_kernel()
                AlphaSolveConfig.configure_wolfram_availability(probe.available, probe.reason)
                startup["wolfram"] = {
                    "available": probe.available,
                    "reason": probe.reason,
                    "kernel_path": probe.kernel_path,
                }
                if renderer is not None:
                    level = "INFO" if probe.available else "WARNING"
                    renderer.log(None, probe.reason, module="wolfram", level=level)
            execution_gateway = self.execution_gateway_override or ExecutionGateway(
                python_workers=self.tool_executor_size,
                wolfram_enabled=AlphaSolveConfig.WOLFRAM_AVAILABLE,
            )
            startup["execution_gateway"] = {
                "python_workers": self.tool_executor_size,
                "wolfram_enabled": AlphaSolveConfig.WOLFRAM_AVAILABLE,
            }
            self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
            for transient_log in ("worker_results.jsonl", "orchestrator_runs.jsonl"):
                path = self.layout.logs_dir / transient_log
                if path.exists():
                    path.unlink()
            (self.layout.logs_dir / "startup.json").write_text(
                json.dumps(startup, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            suite = load_agent_suite_config(self.config_path)
            verifier_scaling_factor = (
                int(self.verifier_scaling_factor_override)
                if self.verifier_scaling_factor_override is not None
                else int(suite.settings.get("verifier_scaling_factor", 1))
            )
            verifier_scaling_factor = max(1, verifier_scaling_factor)
            client_factory = self.client_factory_override or make_openai_client_factory(suite)

            if "knowledge_digest" in suite.subagents:
                init_knowledge_base(self.layout.knowledge_dir, self.layout.read_problem())
                digest_queue = KnowledgeDigestQueue(
                    knowledge_dir=self.layout.knowledge_dir,
                    workspace_dir=self.layout.workspace_dir,
                    suite=suite,
                    client_factory=client_factory,
                    execution_gateway=execution_gateway,
                )
                digest_queue.start()

            result = None
            all_worker_results = []
            for restart_index in range(self.max_orchestrator_restarts):
                if restart_index > 0 and renderer is not None:
                    renderer.log(
                        None,
                        f"orchestrator stopped without solving — restarting (attempt {restart_index + 1}/{self.max_orchestrator_restarts})",
                        module="ralph-loop",
                        level="WARNING",
                    )
                orchestrator = Orchestrator(
                    layout=self.layout,
                    suite=suite,
                    client_factory=client_factory,
                    max_workers=self.max_workers,
                    max_verify_rounds=self.max_verify_rounds,
                    verifier_scaling_factor=verifier_scaling_factor,
                    subagent_max_depth=self.subagent_max_depth,
                    renderer=renderer,
                    execution_gateway=execution_gateway,
                    digest_queue=digest_queue,
                )
                result = orchestrator.run()
                all_worker_results.extend(result.worker_results)
                self._append_orchestrator_run_log(restart_index=restart_index, result=result)
                if result.solution_path is not None:
                    break
            if result is None:
                result = OrchestratorRunResult(final_answer="", trace=[], worker_results=[], solution_path=None)
            elif all_worker_results != result.worker_results:
                result = OrchestratorRunResult(
                    final_answer=result.final_answer,
                    trace=result.trace,
                    worker_results=all_worker_results,
                    solution_path=result.solution_path,
                )
        except Exception as exc:
            if renderer is not None:
                renderer.update_orchestrator_phase("error", status="failed")
                renderer.log(None, str(exc), module="error", level="ERROR")
            self._write_error(exc)
            raise
        finally:
            if digest_queue is not None:
                digest_queue.stop()
            if owns_gateway and execution_gateway is not None:
                execution_gateway.close()
            if renderer is not None:
                renderer.stop()
        (self.layout.logs_dir / "orchestrator_trace.json").write_text(
            json.dumps(result.trace, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.layout.logs_dir / "worker_results.json").write_text(
            json.dumps([_worker_result_to_json(item) for item in result.worker_results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if result.solution_path is not None:
            (self.layout.logs_dir / "solution.json").write_text(
                json.dumps({"solution_path": str(result.solution_path)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return result

    def _write_error(self, exc: Exception) -> None:
        payload: dict[str, Any] = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if isinstance(exc, AgentRunError):
            payload["agent_trace"] = exc.trace
        self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.layout.logs_dir / "error.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_orchestrator_run_log(self, *, restart_index: int, result: OrchestratorRunResult) -> None:
        payload = {
            "attempt": restart_index + 1,
            "final_answer": (result.final_answer or "")[:1000],
            "trace_events": len(result.trace),
            "worker_results": len(result.worker_results),
            "solution_path": str(result.solution_path) if result.solution_path else None,
        }
        with (self.layout.logs_dir / "orchestrator_runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run_alphasolve(**kwargs) -> OrchestratorRunResult:
    return AlphaSolve(**kwargs).run()


def make_openai_client_factory(suite) -> ClientFactory:
    def factory(config: GeneralAgentConfig):
        return OpenAIChatClient(_resolve_model_config(config.model_config, suite=suite))

    return factory


def _resolve_model_config(model_ref: Any, *, suite) -> dict[str, Any]:
    if isinstance(model_ref, dict):
        return _normalize_model_config(model_ref)

    ref = str(model_ref or "").strip()
    if ref and ref in suite.models:
        return _normalize_model_config(dict(suite.models[ref]))

    candidates = []
    if ref:
        candidates.append(ref)
        candidates.append(ref.upper())
        if not ref.upper().endswith("_CONFIG"):
            candidates.append(ref.upper() + "_CONFIG")

    for candidate in candidates:
        if hasattr(AlphaSolveConfig, candidate):
            return dict(getattr(AlphaSolveConfig, candidate))

    if hasattr(AlphaSolveConfig, "GENERATOR_CONFIG"):
        return dict(AlphaSolveConfig.GENERATOR_CONFIG)
    raise ValueError(f"cannot resolve model config: {model_ref}")


def _normalize_model_config(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    api_key_env = out.pop("api_key_env", None)
    if api_key_env and "api_key" not in out:
        out["api_key"] = lambda env=str(api_key_env): os.getenv(env)
    return out


def _worker_result_to_json(result) -> dict[str, Any]:
    return {
        "worker_id": result.worker_id,
        "worker_dir": str(result.worker_dir),
        "status": result.status,
        "summary": result.summary,
        "lemma_file": str(result.lemma_file) if result.lemma_file else None,
        "verified_file": str(result.verified_file) if result.verified_file else None,
        "review_file": str(result.review_file) if result.review_file else None,
        "theorem_check_file": str(result.theorem_check_file) if result.theorem_check_file else None,
        "solved_problem": result.solved_problem,
    }
