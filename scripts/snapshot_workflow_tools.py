"""一次性脚本：把每个 workflow agent 拿到的 ToolDef 列表 dump 成 JSON snapshot。

用途：重构前在主分支跑一次，固化基线；后续重构每个 commit 都用
``tests/test_workflow_tool_snapshots.py`` 比对，确保 description / parameters /
默认值不漂移。

运行方式::

    python scripts/snapshot_workflow_tools.py

会覆盖 ``tests/fixtures/workflow_tool_snapshots/*.json``。
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from alphasolve.agent import (
    AgentConfig,
    ToolRegistry,
    ToolResult,
    Workspace,
    load_agent_suite,
)
from alphasolve.solver.subagent_service import SubagentService
from alphasolve.solver.tool_runtime import (
    build_solver_tool_registry,
    register_execution_tools,
    register_orchestrator_worker_tools,
)
from alphasolve.solver.workspace_access import RoleWorkspaceAccess
PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "alphasolve"


# ---------------------------------------------------------------------------
# 各 agent 在运行时拿到的 RoleWorkspaceAccess 参数与 registry 选项。
# 字段含义与 alphasolve.solver.worker.Worker / orchestrator.Orchestrator /
# curator.CuratorQueue 内的真实构造一一对应；snapshot 必须复刻这些参数才能
# 得到与运行期完全一致的 ToolDef 列表。
# ---------------------------------------------------------------------------
def _generator_access(workspace: Workspace, worker_rel: str) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(
        workspace=workspace,
        worker_rel=worker_rel,
        deny_other_unverified=True,
        single_proposition_file=True,
    )


def _verifier_access(workspace: Workspace, worker_rel: str, *, config_name: str) -> RoleWorkspaceAccess:
    # 复刻 worker._run_verifier_attempt_agent 里的 deny_read_rels 构造。
    verifier_ws_rel = f"{worker_rel}/verifier_workspace"
    deny_read_rels: tuple[str, ...] = (verifier_ws_rel,)
    if config_name == "verifier_citation":
        deny_read_rels = ("knowledge", *deny_read_rels)
    return RoleWorkspaceAccess(
        workspace=workspace,
        worker_rel=worker_rel,
        deny_other_unverified=True,
        deny_read_rels=deny_read_rels,
        deny_read_file_names=("review.md",),
    )


def _reviser_access(workspace: Workspace, worker_rel: str) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(
        workspace=workspace,
        worker_rel=worker_rel,
        deny_other_unverified=True,
        exact_write_rel=f"{worker_rel}/proposition.md",
    )


def _theorem_checker_access(workspace: Workspace, worker_rel: str) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(
        workspace=workspace,
        worker_rel=worker_rel,
        deny_other_unverified=True,
        read_root_rel="verified_propositions",
    )


def _orchestrator_access(workspace: Workspace) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(
        workspace=workspace,
        write_root_rel="verified_propositions",
        destructive_protected_file_names=("index.md",),
        preserve_markdown_file_names_on_rename=True,
    )


def _curator_access(workspace: Workspace) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(
        workspace=workspace,
        read_root_rel="knowledge",
        write_root_rel="knowledge",
        destructive_protected_file_names=("index.md", "common-errors.md"),
    )


def _subagent_default_access(workspace: Workspace) -> RoleWorkspaceAccess:
    # 普通 subagent（compute / reasoning / numerical / research_reviewer）
    # 不需要写入 workspace，read 范围与父 agent 相同。
    return RoleWorkspaceAccess(workspace=workspace)


# 每个 agent 的 (RoleWorkspaceAccess factory, allow_write, allow_manage, allow_delete)。
# 字段语义对照 worker.py / orchestrator.py / curator.py 内的真实运行期参数。
def _agent_setup(name: str, workspace: Workspace, worker_rel: str) -> tuple[RoleWorkspaceAccess, bool, bool, bool]:
    verifier_configs = {
        "verifier",
        "verifier_adversarial",
        "verifier_citation",
        "verifier_failure_modes",
        "verifier_format_references",
        "verifier_premise_chain",
        "verifier_stepwise",
    }
    if name == "generator":
        return _generator_access(workspace, worker_rel), True, False, False
    if name in verifier_configs:
        return _verifier_access(workspace, worker_rel, config_name=name), False, False, False
    if name == "reviser":
        return _reviser_access(workspace, worker_rel), True, False, False
    if name == "theorem_checker":
        return _theorem_checker_access(workspace, worker_rel), False, False, False
    if name == "orchestrator":
        return _orchestrator_access(workspace), True, True, False
    if name == "curator":
        return _curator_access(workspace), True, True, True
    # 其余 subagent：reasoning / compute / numerical / research_reviewer。
    return _subagent_default_access(workspace), False, False, False


def _register_compute_subagent_extra_tools(registry: ToolRegistry) -> None:
    """为 compute / numerical 子 agent 补齐 RunPython / RunWolfram 描述。

    描述文本必须与 ``alphasolve.solver.subagent_service.SubagentService._build_subagent_registry``
    内对应注册一字一句保持一致。
    """
    register_execution_tools(
        registry,
        run_python_handler=lambda _args: ToolResult(""),
        run_wolfram_handler=lambda _args: ToolResult(""),
    )


def _register_orchestrator_extra_tools(registry: ToolRegistry) -> None:
    """为 orchestrator 补齐 SpawnWorker / TaskOutput 描述，使其能被 ToolRegistry.tool_defs 看到。

    描述文本必须与 ``alphasolve.solver.orchestrator.Orchestrator._build_registry``
    内 ``SpawnWorker`` / ``TaskOutput`` 的注册一字一句保持一致；后续 commit 改这两处
    必须同步修改这里。
    """
    from alphasolve.solver.worker import Worker as _Worker  # noqa: F401  仅用于让 codegraph 看到依赖
    # WorkerManager 的默认等待时长嵌在 TaskOutput 描述里，需要导入。
    from alphasolve.solver.orchestrator import WorkerManager

    register_orchestrator_worker_tools(
        registry,
        spawn_handler=lambda _args: ToolResult(""),
        wait_handler=lambda _args: ToolResult(""),
        default_wait_timeout_seconds=WorkerManager.DEFAULT_WAIT_TIMEOUT_SECONDS,
    )


def _build_registry_for_agent(
    name: str,
    config: AgentConfig,
    workspace_root: Path,
    suite: Any,
) -> list[dict]:
    """复刻第三层在运行时为某个 agent 构造的最终工具集，dump 为可序列化形式。"""
    workspace = Workspace(root=workspace_root)
    worker_rel = "unverified_propositions/prop-snapshot"
    access, allow_write, allow_manage, allow_delete = _agent_setup(name, workspace, worker_rel)
    extra_registrars = []
    if name == "orchestrator":
        extra_registrars.append(_register_orchestrator_extra_tools)
    if name in {"compute_subagent", "numerical_experiment_subagent"}:
        extra_registrars.append(_register_compute_subagent_extra_tools)

    # SubagentService 仅用于让 Agent 工具的 enum 与运行期一致；client_factory
    # 永远不会被调用，因为我们只 dump 工具描述，不真正运行 agent。
    subagent_service = SubagentService(
        suite=suite,
        client_factory=lambda _cfg: None,  # type: ignore[arg-type, return-value]
        max_depth=2,
    )
    registry = build_solver_tool_registry(
        access,
        agent_config=config,
        dispatcher=subagent_service,
        extra_registrars=tuple(extra_registrars),
    )
    tool_defs = registry.tool_defs(
        enabled=list(config.tools),
        tool_parameters=config.tool_parameters,
        tool_descriptions=config.tool_descriptions,
    )
    return [asdict(td) for td in tool_defs]


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    suite_path = Path(PACKAGE_ROOT) / "solver" / "config"
    suite = load_agent_suite(suite_path)

    snapshots_dir = repo_root / "tests" / "fixtures" / "workflow_tool_snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir)
        # 创建 agent/subagent workspace 可能引用的子目录，避免 RoleWorkspaceAccess 解析路径时报错。
        for sub in (
            "verified_propositions",
            "unverified_propositions",
            "unverified_propositions/prop-snapshot",
            "unverified_propositions/prop-snapshot/verifier_workspace",
            "knowledge",
            "knowledge/references",
        ):
            (workspace_root / sub).mkdir(parents=True, exist_ok=True)

        agents = {**suite.agents, **suite.subagents}
        total_count = len(agents)
        ok_count = 0
        for name, config in agents.items():
            try:
                snapshot = _build_registry_for_agent(name, config, workspace_root, suite)
            except Exception as exc:
                print(f"[skip] {name}: {exc}")
                continue
            target = snapshots_dir / f"{name}.json"
            target.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"[ok]  {name} -> {target.relative_to(repo_root).as_posix()}")
            ok_count += 1

        print(f"\n{ok_count}/{total_count} agents serialized.")
        if ok_count < total_count:
            print(
                f"WARNING: {total_count - ok_count} agents were skipped; "
                "their old fixtures (if any) remain on disk.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
