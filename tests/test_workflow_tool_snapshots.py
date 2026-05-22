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
# Import shim：在 Task 1/2/9 等搬迁过程中支持新旧两套模块路径。脚本
# (``scripts/snapshot_workflow_tools.py``) 自己也会做相应调整，但 test 不再
# 依赖脚本——通过这里的 try/except 直接拿到所需 symbol。
# ---------------------------------------------------------------------------
try:
    from alphasolve.agents.general import (
        GeneralAgentConfig,
        ToolRegistry,
        ToolResult,
        Workspace,
        load_agent_suite_config,
    )
    from alphasolve.agents.team.tools import (
        RoleWorkspaceAccess,
        SubagentService,
        build_workspace_tool_registry,
        register_agent_tool,
    )
    from alphasolve.agents.team.orchestrator import WorkerManager
    _OLD_PATHS = True
except ImportError:  # pragma: no cover - 兼容 Task 1/2/9 搬迁后的新路径
    from alphasolve.agent import (  # type: ignore[no-redef]
        GeneralAgentConfig,
        ToolRegistry,
        ToolResult,
        Workspace,
    )
    from alphasolve.agent import load_agent_suite as load_agent_suite_config  # type: ignore[no-redef]
    from alphasolve.workflow.workspace_access import RoleWorkspaceAccess  # type: ignore[no-redef]
    from alphasolve.workflow.subagent_service import SubagentService  # type: ignore[no-redef]
    from alphasolve.workflow.workflow_tools import build_workspace_tool_registry  # type: ignore[no-redef]
    from alphasolve.agent.tools import register_agent_tool  # type: ignore[no-redef]
    # WorkerManager 等 worker 内部 symbol 在新结构下可能换位置——届时再调整。
    from alphasolve.workflow.worker_manager import WorkerManager  # type: ignore[no-redef]
    _OLD_PATHS = False

from alphasolve.config.agent_config import PACKAGE_ROOT

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
    return RoleWorkspaceAccess(workspace=workspace)


def _agent_setup(name: str, workspace: Workspace, worker_rel: str) -> tuple[RoleWorkspaceAccess, bool, bool, bool]:
    verifier_configs = {
        "verifier",
        "verifier_adversarial",
        "verifier_citation",
        "verifier_failure_modes",
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
    registry.register(
        name="RunPython",
        description=(
            "Executes Python code in a persistent in-memory environment without filesystem access.\n\n"
            "Usage:\n"
            "- Run Python/SymPy/NumPy/SciPy code for symbolic/numeric computation.\n"
            "- The Python environment persists across calls within the same session.\n"
            "- No filesystem access is permitted; use file tools separately if needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python code to execute."},
            },
            "required": ["code"],
        },
        handler=lambda _args: ToolResult(""),
    )
    registry.register(
        name="RunWolfram",
        description="Execute Wolfram Language code in a short-lived Wolfram session when Wolfram is available.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Wolfram Language code to execute."},
            },
            "required": ["code"],
        },
        handler=lambda _args: ToolResult(""),
    )


def _register_orchestrator_extra_tools(registry: ToolRegistry) -> None:
    """为 orchestrator 补齐 SpawnWorker / TaskOutput 描述。"""
    registry.register(
        name="SpawnWorker",
        description=(
            "Start one worker and return immediately; this tool does not wait for the worker to finish.\n\n"
            "Worker lifecycle:\n"
            "- The worker first runs generator to draft one candidate proposition.\n"
            "- It then runs verifier; if verification fails and rounds remain, it runs reviser and repeats verifier -> reviser.\n"
            "- After verification, theorem-checking decides whether the verified proposition resolves the original problem.\n\n"
            "Return content is JSON containing whether a worker was spawned, plus active_count, active_worker_ids, active_workers, max_workers, and available_worker_slots. "
            "If the parallelism limit has been reached, call TaskOutput before spawning more workers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hint": {
                    "type": "string",
                    "description": (
                        "Optional targeted hint for this worker only. Suggest a direction, method, "
                        "branch, local target, or bootstrap assumption. This is different from the user's hint.md."
                    ),
                },
            },
            "required": [],
        },
        handler=lambda _args: ToolResult(""),
    )
    registry.register(
        name="TaskOutput",
        description=(
            "Wait until one active worker finishes, or until the timeout is reached.\n\n"
            "Use this tool to collect worker lifecycle results. If the maximum number of active workers has been reached, call TaskOutput before spawning more workers.\n\n"
            "Return content is JSON. It always includes completed, active_count, active_worker_ids, active_workers, max_workers, and available_worker_slots. "
            "It may include timed_out when no worker finishes before the timeout; solved and solution_path when the original problem is solved; "
            "human_expert_updates when hint.md or knowledge/references changed during the run; and verified_propositions_organization when verified proposition directories should be organized before more spawning."
        ),
        parameters={
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Maximum seconds to wait before returning active worker status.",
                    "default": WorkerManager.DEFAULT_WAIT_TIMEOUT_SECONDS,
                    "minimum": 1200,
                    "maximum": 3600,
                },
            },
            "required": [],
        },
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
    registry = build_workspace_tool_registry(
        access,
        allow_write=allow_write,
        allow_manage=allow_manage,
        allow_delete=allow_delete,
    )
    if name == "orchestrator":
        _register_orchestrator_extra_tools(registry)
    if name in {"compute_subagent", "numerical_experiment_subagent"}:
        _register_compute_subagent_extra_tools(registry)

    # SubagentService 仅用于让 Agent 工具的 enum 与运行期一致；client_factory
    # 永远不会被调用，因为我们只 dump 工具描述，不真正运行 agent。
    subagent_service = SubagentService(
        suite=suite,
        client_factory=lambda _cfg: None,  # type: ignore[arg-type, return-value]
        max_depth=2,
    )
    register_agent_tool(
        registry,
        agent_config=config,
        subagent_service=subagent_service,
    )
    tool_defs = registry.tool_defs(
        enabled=list(config.tools),
        tool_parameters=config.tool_parameters,
    )
    return [asdict(td) for td in tool_defs]


@pytest.mark.parametrize("snapshot_path", _snapshot_files(), ids=lambda p: p.stem)
def test_workflow_tool_snapshot_matches(snapshot_path: Path) -> None:
    """逐个 agent 比对：当前代码生成的 ToolDef 必须与 fixture JSON 字节一致。"""
    name = snapshot_path.stem
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))

    suite_path = Path(PACKAGE_ROOT) / "config"
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
