"""回归测试：每个 workflow agent 重构后拿到的 ToolDef 列表必须与 fixture 字节一致。

如果故意要改 description（比如 commit 8 重写 YAML 用 tool_descriptions.suffix），
跑 ``python scripts/snapshot_workflow_tools.py`` 重新生成 fixture，并在 commit message
里说明每一处变化。
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "workflow_tool_snapshots"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _snapshot_files() -> list[Path]:
    return sorted(_FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("snapshot_path", _snapshot_files(), ids=lambda p: p.stem)
def test_workflow_tool_snapshot_matches(snapshot_path: Path) -> None:
    """逐个 agent 比对：当前代码生成的 ToolDef 必须与 fixture JSON 字节一致。"""
    # 让 scripts/ 进入 sys.path 以便 import snapshot 脚本，复用其 _build_registry_for_agent。
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))

    # 延迟 import：后续 commit 会改 import 路径，但 snapshot 脚本本身的 import
    # 在重构后也会被同步更新，所以这里直接 import 脚本里的辅助函数即可。
    from snapshot_workflow_tools import _build_registry_for_agent  # type: ignore
    try:
        from alphasolve.agents.general import load_agent_suite_config
    except ImportError:  # pragma: no cover - 兼容后续 commit 改名情况
        from alphasolve.agent import load_agent_suite as load_agent_suite_config  # type: ignore

    from alphasolve.config.agent_config import PACKAGE_ROOT

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
