"""复用正式 generator 装配的交互工具测试入口。"""
from __future__ import annotations

from pathlib import Path

from alphasolve.agent import Agent, AgentSuite, Workspace
from alphasolve.agent.ui.cli_app import AgentApp

from .execution import ExecutionGateway
from .policy import SolverPolicy
from .role import Role, RoleContext


class GeneratorAgentApp(AgentApp):
    """把当前目录作为 workspace，使用指定 worker 的真实工具权限。"""

    def __init__(
        self,
        *,
        suite: AgentSuite,
        worker_dir: str | Path = "unverified_propositions/agent-test",
        policy: SolverPolicy | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.suite = suite
        self.policy = policy or SolverPolicy.from_settings(suite.settings)
        self.worker_dir = Workspace(self.project_dir).resolve(worker_dir)
        self.worker_rel = self.worker_dir.relative_to(self.project_dir).as_posix()
        if not self.worker_rel.startswith("unverified_propositions/"):
            raise ValueError("--worker-dir must be inside unverified_propositions/")
        self._execution_gateway: ExecutionGateway | None = None

    def run(self) -> None:
        self.console.print(f"generator worker: {self.worker_rel}", markup=False)
        super().run()

    def run_once(self, prompt: str, **kwargs):
        """与正式 worker 一样，在任务中明确告知角色分配到的文件位置。"""
        task = (
            f"Workspace root: {self.project_dir}\n"
            f"Assigned worker directory: {self.worker_rel}\n"
            f"Assigned proposition file: {self.worker_rel}/proposition.md\n\n"
            f"{prompt}"
        )
        return super().run_once(task, **kwargs)

    def _build_agent(self) -> Agent:
        self._execution_gateway = ExecutionGateway(python_workers=self.policy.tool_executor_size)
        ctx = RoleContext(
            workspace=Workspace(self.project_dir),
            worker_rel=self.worker_rel,
            worker_dir=self.worker_dir,
            suite=self.suite,
            client_factory=self.client_factory,
            subagent_max_depth=self.policy.subagent_max_depth,
            execution_gateway=self._execution_gateway,
            curator_queue=None,
            log_session=None,
            stop_event=self.stop_event,
            event_sink_factory=lambda label: None,
            trace=[],
        )
        return Role.for_generator(ctx).agent

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._execution_gateway is not None:
                self._execution_gateway.close()
                self._execution_gateway = None
