from __future__ import annotations

import json
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from alphasolve.agent import AgentRunError, AgentConfig, load_agent_suite
from alphasolve.solver.wolfram_state import AlphaSolveConfig
from alphasolve.solver.execution import ExecutionGateway
from alphasolve.solver.wolfram_probe import check_wolfram_kernel
from alphasolve.solver.logging.log_session import LogSession
from alphasolve.solver.ui.team_renderer import PropositionTeamRenderer
from .difficulty_dag import DifficultyDagStore
from .curator import CuratorQueue, CuratorTask, init_knowledge_base
from .cold_start import ColdStartRuntime
from .orchestrator import Orchestrator, OrchestratorRunResult, verified_count
from .policy import SolverPolicy
from .project import ProjectLayout
from .client_factory import ClientFactory


class AlphaSolve:
    def __init__(
        self,
        *,
        project_dir: str | Path,
        problem: str | Path = "problem.md",
        hint: str | Path | None = None,
        config_path: str | Path | None = None,
        policy: SolverPolicy | None = None,
        max_workers: int | None = None,
        max_verify_rounds: int | None = None,
        verifier_scaling_factor: int | None = None,
        subagent_max_depth: int | None = None,
        client_factory: ClientFactory,
        prime_wolfram: bool = True,
        print_to_console: bool = True,
        tool_executor_size: int | None = None,
        execution_gateway: ExecutionGateway | None = None,
        max_orchestrator_restarts: int | None = None,
        debug: bool = False,
    ) -> None:
        self.layout = ProjectLayout.create(project_dir, problem=problem, hint=hint)
        self.config_path = Path(config_path).resolve() if config_path else Path(__file__).resolve().parent.parent / "solver" / "config"
        self.policy_override = policy
        self.policy_overrides = {
            "max_workers": max_workers,
            "max_verify_rounds": max_verify_rounds,
            "verifier_scaling_factor": verifier_scaling_factor,
            "subagent_max_depth": subagent_max_depth,
            "tool_executor_size": tool_executor_size,
            "max_orchestrator_restarts": max_orchestrator_restarts,
        }
        self.policy: SolverPolicy | None = None
        self.client_factory = client_factory
        self.prime_wolfram = prime_wolfram
        self.print_to_console = print_to_console
        self.execution_gateway_override = execution_gateway
        self.debug = debug
        self._stop_event = threading.Event()
        self._renderer: PropositionTeamRenderer | None = None

    def cancel(self) -> None:
        self._stop_event.set()

    def run(self) -> OrchestratorRunResult:
        renderer = PropositionTeamRenderer(screen=True, stop_event=self._stop_event, problem_name=self.layout.problem_path.parent.name) if self.print_to_console else None
        self._renderer = renderer
        execution_gateway: ExecutionGateway | None = None
        owns_gateway = self.execution_gateway_override is None
        curator_queue: CuratorQueue | None = None
        log_session: LogSession | None = None
        if renderer is not None:
            renderer.start()
            renderer.update_orchestrator_phase("startup", status="running")
        try:
            self.layout.ensure()
            # 统一运行日志（alphasolve_run.log）始终启用；仅详细 trace 日志受
            # --debug 控制（detail=self.debug）。这样默认就能看到每次 LLM 调用的
            # token 消耗（区分输入/输出）与可见推理摘要，而不必开 --debug。
            log_session = LogSession(base_dir=str(self.layout.logs_dir), detail=self.debug)
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
            if renderer is not None:
                renderer.update_orchestrator_phase("resume checks", status="running")
            startup["workspace_inputs"] = self.layout.sync_workspace_inputs()
            if renderer is not None:
                renderer.log(None, "workspace problem.md and hint.md synced from project root", module="startup")
            suite = load_agent_suite(self.config_path)
            base_policy = self.policy_override or SolverPolicy.from_settings(suite.settings)
            runtime_policy = base_policy.with_overrides(**self.policy_overrides)
            self.policy = runtime_policy
            startup["solver_policy"] = runtime_policy.to_dict()

            execution_gateway = self.execution_gateway_override or ExecutionGateway(
                python_workers=runtime_policy.tool_executor_size,
                wolfram_enabled=AlphaSolveConfig.WOLFRAM_AVAILABLE,
            )
            startup["execution_gateway"] = {
                "python_workers": runtime_policy.tool_executor_size,
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
            client_factory = self.client_factory

            if "curator" in suite.subagents:
                init_knowledge_base(self.layout.knowledge_dir, self.layout.read_problem())
                curator_queue = CuratorQueue(
                    knowledge_dir=self.layout.knowledge_dir,
                    workspace_dir=self.layout.workspace_dir,
                    suite=suite,
                    client_factory=client_factory,
                    execution_gateway=execution_gateway,
                    log_session=log_session,
                    stop_event=self._stop_event,
                    renderer=renderer,
                    policy=runtime_policy,
                )
                curator_queue.start()
                difficulty_dag = DifficultyDagStore(
                    self.layout.workspace_dir,
                    policy=runtime_policy.difficulty_dag,
                )
                for checkpoint_id in difficulty_dag.pending_checkpoint_ids():
                    task_kind = difficulty_dag.checkpoint_task_kind(checkpoint_id)
                    artifact_path = difficulty_dag.checkpoint_artifact_path(checkpoint_id)
                    curator_queue.submit(
                        CuratorTask(
                            trace_segment=[],
                            source_label=f"{task_kind}-recovery/{checkpoint_id}",
                            task_kind=task_kind,
                            artifact_path=artifact_path,
                            audit_path=(artifact_path.parent / "audit.md") if task_kind == "portfolio_checkpoint" else None,
                        )
                    )

            result = None
            all_worker_results = []
            for restart_index in range(runtime_policy.max_orchestrator_restarts):
                if self._stop_event.is_set():
                    break
                if restart_index > 0 and renderer is not None:
                    renderer.log(
                        None,
                        f"orchestrator stopped without solving — restarting (attempt {restart_index + 1}/{runtime_policy.max_orchestrator_restarts})",
                        module="ralph-loop",
                        level="WARNING",
                    )
                session_id = f"ralph-{restart_index + 1}-{uuid.uuid4().hex[:8]}"
                cold_start_runtime = ColdStartRuntime(
                    layout=self.layout,
                    max_workers=runtime_policy.max_workers,
                    threshold=runtime_policy.cold_start_verified_proposition_threshold,
                    session_id=session_id,
                    stop_event=self._stop_event,
                )
                orchestrator = Orchestrator(
                    layout=self.layout,
                    suite=suite,
                    client_factory=client_factory,
                    policy=runtime_policy,
                    renderer=renderer,
                    execution_gateway=execution_gateway,
                    curator_queue=curator_queue,
                    log_session=log_session,
                    stop_event=self._stop_event,
                    session_id=session_id,
                    cold_start_runtime=cold_start_runtime,
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
            if curator_queue is not None:
                curator_queue.stop()
            if owns_gateway and execution_gateway is not None:
                execution_gateway.close()
            if renderer is not None:
                renderer.stop()
            # 统一运行日志跨 orchestrator restart 存活，故在 run 级 finally 收尾关闭
            # （停止后台汇总线程并写最终汇总）；此时 workers/subagents/curator 均已结束。
            if log_session is not None:
                log_session.close_run_log()
        if curator_queue is not None and curator_queue.fatal_error is not None:
            failure = curator_queue.fatal_error
            result = OrchestratorRunResult(
                final_answer=f"curator stopped: {failure}",
                trace=[*result.trace, {
                    "type": "run_error",
                    "agent": "curator",
                    "failure_kind": failure.failure_kind,
                    "error": str(failure),
                }],
                worker_results=result.worker_results,
                solution_path=result.solution_path,
            )
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


def _worker_result_to_json(result) -> dict[str, Any]:
    return {
        "worker_id": result.worker_id,
        "worker_dir": str(result.worker_dir),
        "status": result.status,
        "summary": result.summary,
        "proposition_file": str(result.proposition_file) if result.proposition_file else None,
        "verified_file": str(result.verified_file) if result.verified_file else None,
        "review_file": str(result.review_file) if result.review_file else None,
        "theorem_check_file": str(result.theorem_check_file) if result.theorem_check_file else None,
        "difficulty_id": result.difficulty_id,
        "solved_problem": result.solved_problem,
    }
