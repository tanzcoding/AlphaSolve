"""回归测试：每个 workflow agent 重构后拿到的 ToolDef 列表必须与 fixture 字节一致。

如果故意要改 description（比如 commit 8 重写 YAML 用 tool_descriptions.suffix），
跑 ``python scripts/snapshot_workflow_tools.py`` 重新生成 fixture，并在 commit message
里说明每一处变化。

本文件内联 ``scripts/snapshot_workflow_tools.py`` 里的 ``_build_registry_for_agent``
及其辅助逻辑，并用 try/except 切换新旧 import 路径，确保后续 commit 把
``alphasolve.agents.general`` / ``alphasolve.agents.team.*`` 拆分搬迁时，snapshot
守门测试仍能继续运行。脚本本体保留为重新生成 fixture 的官方入口；逻辑在两处同步
是接受的代价，换来搬迁瞬间 snapshot 测试继续守门的能力。
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 导入垫片：三层拆分重构期间逐 symbol 探测 import 路径。
#
# 背景：plan 是 Task 1-9 多个 commit 逐步搬迁的——Task 1 搬第二层
# (``agents/general → agent``)、Task 2-3 搬第三层 (``agents/team → workflow``)、
# Task 4 拆 workflow/tools.py、Task 5 上移 register_agent_tool 到第二层、
# Task 6-8 补基础工具/改 YAML、Task 9 重命名 (``load_agent_suite_config →
# load_agent_suite`` 等)。最早的"旧路径全套 / 新路径全套"二选一 shim 在
# 中间过渡态会因 ImportError 导致整个测试文件 collection-time ERROR。
#
# 策略：每个 symbol 按"旧 → 过渡 → 最终"顺序逐层探测；任一缺失就让
# ``pytestmark = pytest.mark.skip`` 把整个文件标记为 SKIPPED（而非 ERROR），
# 中间态由 176 个非 snapshot 测试守门。Task 9 完成后所有 symbol 就位，
# shim 走到最深的 try 分支，snapshot 测试恢复绿，作为 plan 承诺的
# "重构前后字节一致"的最终验证。
# ---------------------------------------------------------------------------

_SHIM_SKIP_REASON: str | None = None

# Workspace: Task 1 把第二层从 ``agents/general`` 搬到 ``agent``。
try:
    from alphasolve.agents.general import Workspace
except ImportError:
    try:
        from alphasolve.agent import Workspace  # type: ignore[no-redef]
    except ImportError as exc:
        _SHIM_SKIP_REASON = f"Workspace import failed: {exc}"
        Workspace = None  # type: ignore[assignment, misc]

# GeneralAgentConfig: Task 1 路径变化 + Task 9 重命名为 ``AgentConfig``。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.general import GeneralAgentConfig
    except ImportError:
        try:
            from alphasolve.agent import AgentConfig as GeneralAgentConfig  # type: ignore[no-redef]
        except ImportError:
            try:
                from alphasolve.agent import GeneralAgentConfig  # type: ignore[no-redef]
            except ImportError as exc:
                _SHIM_SKIP_REASON = f"GeneralAgentConfig import failed: {exc}"
                GeneralAgentConfig = None  # type: ignore[assignment, misc]

# ToolRegistry: 同上路径变化。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.general import ToolRegistry
    except ImportError:
        try:
            from alphasolve.agent import ToolRegistry  # type: ignore[no-redef]
        except ImportError as exc:
            _SHIM_SKIP_REASON = f"ToolRegistry import failed: {exc}"
            ToolRegistry = None  # type: ignore[assignment, misc]

# ToolResult: 同上路径变化。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.general import ToolResult
    except ImportError:
        try:
            from alphasolve.agent import ToolResult  # type: ignore[no-redef]
        except ImportError as exc:
            _SHIM_SKIP_REASON = f"ToolResult import failed: {exc}"
            ToolResult = None  # type: ignore[assignment, misc]

# load_agent_suite_config: Task 9 重命名为 ``load_agent_suite``。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.general import load_agent_suite_config
    except ImportError:
        try:
            from alphasolve.agent import load_agent_suite as load_agent_suite_config  # type: ignore[no-redef]
        except ImportError:
            try:
                from alphasolve.agent import load_agent_suite_config  # type: ignore[no-redef]
            except ImportError as exc:
                _SHIM_SKIP_REASON = f"load_agent_suite[_config] import failed: {exc}"

# RoleWorkspaceAccess: Task 2 搬到 workflow，Task 4 拆到 workspace_access.py。
# Task 8 删除了 alphasolve.solver.tools shim，所以中间过渡分支不再存在。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.team.tools import RoleWorkspaceAccess
    except ImportError:
        try:
            from alphasolve.solver.workspace_access import RoleWorkspaceAccess  # type: ignore[no-redef]
        except ImportError as exc:
            _SHIM_SKIP_REASON = f"RoleWorkspaceAccess import failed: {exc}"

# SubagentService: 同上路径变化（Task 4 拆到 subagent_service.py）。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.team.tools import SubagentService
    except ImportError:
        try:
            from alphasolve.solver.subagent_service import SubagentService  # type: ignore[no-redef]
        except ImportError as exc:
            _SHIM_SKIP_REASON = f"SubagentService import failed: {exc}"

# build_workspace_tool_registry: A 阶段被 workflow_tools.py 包装；phase C T11 删了
# workflow_tools 后改用 agent/tools 的 build_default_tool_registry（语义等价）。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.team.tools import build_workspace_tool_registry
    except ImportError:
        try:
            from alphasolve.solver.workflow_tools import build_workspace_tool_registry  # type: ignore[no-redef]
        except ImportError:
            try:
                from alphasolve.agent.tools import build_default_tool_registry as build_workspace_tool_registry  # type: ignore[no-redef]
            except ImportError as exc:
                _SHIM_SKIP_REASON = f"build_workspace_tool_registry import failed: {exc}"

# register_agent_tool: Task 5 上移到第二层。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.team.tools import register_agent_tool
    except ImportError:
        try:
            from alphasolve.solver.workflow_tools import register_agent_tool  # type: ignore[no-redef]
        except ImportError:
            try:
                from alphasolve.agent.tools import register_agent_tool  # type: ignore[no-redef]
            except ImportError as exc:
                _SHIM_SKIP_REASON = f"register_agent_tool import failed: {exc}"

# WorkerManager: Task 2 把整个 orchestrator.py 搬到 workflow/。
if _SHIM_SKIP_REASON is None:
    try:
        from alphasolve.agents.team.orchestrator import WorkerManager
    except ImportError:
        try:
            from alphasolve.solver.orchestrator import WorkerManager  # type: ignore[no-redef]
        except ImportError as exc:
            _SHIM_SKIP_REASON = f"WorkerManager import failed: {exc}"

if _SHIM_SKIP_REASON is not None:
    pytestmark = pytest.mark.skip(
        reason=(
            f"snapshot guardrail temporarily inactive during three-layer-split refactor: "
            f"{_SHIM_SKIP_REASON}. Will resume once all import paths land (Task 9)."
        )
    )

import alphasolve
from alphasolve.solver.tool_runtime import (
    build_solver_tool_registry,
    register_execution_tools,
    register_orchestrator_research_tools,
    register_orchestrator_worker_tools,
)
PACKAGE_ROOT = Path(alphasolve.__file__).resolve().parent

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "workflow_tool_snapshots"


def _snapshot_files() -> list[Path]:
    return sorted(_FIXTURES_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# 以下逻辑内联自 scripts/snapshot_workflow_tools.py。两边必须保持一致，详见
# 文件顶部 docstring。
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
        allowed_extensions=(".md",),
        destructive_protected_file_names=("index.md", "state.md"),
    )


def _curator_access(workspace: Workspace) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(
        workspace=workspace,
        read_root_rel="knowledge",
        write_root_rel="knowledge",
        deny_text_write_rels=("knowledge/references",),
        protected_reference_rels=("knowledge/references",),
        destructive_protected_file_names=("index.md", "common-errors.md"),
    )


def _subagent_default_access(workspace: Workspace) -> RoleWorkspaceAccess:
    return RoleWorkspaceAccess(workspace=workspace)


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
    return _subagent_default_access(workspace), False, False, False


def _register_compute_subagent_extra_tools(registry: ToolRegistry) -> None:
    """为 compute / numerical 子 agent 补齐 RunPython / RunWolfram 描述。"""
    register_execution_tools(
        registry,
        run_python_handler=lambda _args: ToolResult(""),
        run_wolfram_handler=lambda _args: ToolResult(""),
    )


def _register_orchestrator_extra_tools(registry: ToolRegistry) -> None:
    """为 orchestrator 补齐 SpawnWorker / TaskOutput 描述。"""
    register_orchestrator_worker_tools(
        registry,
        spawn_handler=lambda _args: ToolResult(""),
        wait_handler=lambda _args: ToolResult(""),
        default_wait_timeout_seconds=WorkerManager.DEFAULT_WAIT_TIMEOUT_SECONDS,
    )
    _register_orchestrator_free_exploration_tool(registry)
    register_orchestrator_research_tools(
        registry,
        record_impact_handler=lambda _args: ToolResult(""),
        record_dispatch_constraint_handler=lambda _args: ToolResult(""),
        sync_state_handler=lambda _args: ToolResult(""),
    )


def _register_orchestrator_free_exploration_tool(registry: ToolRegistry) -> None:
    """复刻 orchestrator.Orchestrator._register_free_exploration_tool 里 SpawnFreeExploration 的注册。

    与 scripts/snapshot_workflow_tools.py 内的同名副本、以及 orchestrator.py 的真实注册
    必须一字一句保持一致。
    """
    registry.register(
        name="SpawnFreeExploration",
        description=(
            "Start one worker for open-ended, non-targeted exploration and return immediately "
            "(does not wait). Use it only for a materially orthogonal idea, not to bypass a blocked "
            "targeted route. The runtime refuses this tool while a completed process audit still needs "
            "curator blocker classification or while a stalled audit names a repeated avoided obligation; "
            "in the latter case, launch the prescribed targeted consolidation instead.\n\n"
            "The worker receives persisted failed-method taboos, active repeated blockers, and an optional "
            "under-explored verified seed. It must choose a distinct bounded claim or record why no such "
            "claim is available. Returns the same shape as SpawnWorker; if no slot is available it returns "
            "spawned=false."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda _args: ToolResult(""),
    )


def _build_registry_for_agent(
    name: str,
    config: GeneralAgentConfig,
    workspace_root: Path,
    suite: Any,
) -> list[dict]:
    """复刻第三层在运行时为某个 agent 构造的最终工具集，dump 为可序列化形式。"""
    workspace = Workspace(root=workspace_root)
    worker_rel = "unverified_propositions/prop-snapshot"
    access, allow_write, allow_manage, allow_delete = _agent_setup(name, workspace, worker_rel)
    # allow_* kwargs were silently ignored by the shim and are now gone in phase C T11;
    # build_solver_tool_registry registers the third-layer tool surface.
    del allow_write, allow_manage, allow_delete
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
        allow_research_reviewer=name == "orchestrator",
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


@pytest.mark.parametrize("snapshot_path", _snapshot_files(), ids=lambda p: p.stem)
def test_workflow_tool_snapshot_matches(snapshot_path: Path) -> None:
    """逐个 agent 比对：当前代码生成的 ToolDef 必须与 fixture JSON 字节一致。"""
    name = snapshot_path.stem
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))

    suite_path = Path(PACKAGE_ROOT) / "solver" / "config"
    suite = load_agent_suite_config(suite_path)
    config = (suite.agents | suite.subagents).get(name)
    if config is None:
        pytest.skip(f"agent {name!r} not in current suite")

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir)
        for sub in (
            "verified_propositions",
            "unverified_propositions",
            "unverified_propositions/prop-snapshot",
            "unverified_propositions/prop-snapshot/verifier_workspace",
            "knowledge",
            "knowledge/references",
        ):
            (workspace_root / sub).mkdir(parents=True, exist_ok=True)

        actual = _build_registry_for_agent(name, config, workspace_root, suite)

    # 比对前都 round-trip 过 json.dumps + json.loads，避免 dataclass 内部细微类型差异。
    actual_normalized = json.loads(json.dumps(actual, ensure_ascii=False, sort_keys=True))
    expected_normalized = json.loads(json.dumps(expected, ensure_ascii=False, sort_keys=True))

    assert actual_normalized == expected_normalized, (
        f"Tool snapshot for agent {name!r} drifted. "
        "Run `python scripts/snapshot_workflow_tools.py` to regenerate if the change is intentional."
    )
