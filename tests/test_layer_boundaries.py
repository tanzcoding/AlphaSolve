"""Phase C 层边界 import-graph 测试。

用 ast.parse 扫所有 .py 文件的 Import / ImportFrom 节点，断言：
1. agent/ 下任何 .py 都不能 import alphasolve.solver 或其子模块
2. llm/ 下任何 .py 都不能 import alphasolve.solver 或 alphasolve.agent
3. agent/ 下任何 .py 都不能 import alphasolve.llm.providers.* 或 alphasolve.llm.config.*
   （沿用 A 阶段已建立的不变式）
"""
from __future__ import annotations

import ast
from pathlib import Path

import alphasolve


PACKAGE_ROOT = Path(alphasolve.__file__).parent


def _collect_imports(py_file: Path) -> list[str]:
    """返回 py_file 中所有 import 模块的 fully-qualified name 列表。

    `import a.b` → ["a.b"]
    `from a.b import c` → ["a.b"]
    `from .x import y` (level=1, module="x") → 跳过相对 import（包内不算跨层）
    """
    source = py_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(py_file))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                continue  # 相对 import 不跨层（同包内）
            if node.module:
                imports.append(node.module)
    return imports


def _iter_py_files(subdir: str) -> list[Path]:
    base = PACKAGE_ROOT / subdir
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def test_solver_not_imported_by_agent():
    """agent/ 下任何 .py 都不能 import alphasolve.solver 或其子模块。"""
    violations: list[tuple[Path, str]] = []
    for py_file in _iter_py_files("agent"):
        for imp in _collect_imports(py_file):
            if imp == "alphasolve.solver" or imp.startswith("alphasolve.solver."):
                violations.append((py_file, imp))
    assert not violations, (
        "agent/ must not import alphasolve.solver.*; violations:\n"
        + "\n".join(f"  {p.relative_to(PACKAGE_ROOT)} imports {imp}" for p, imp in violations)
    )


def test_solver_not_imported_by_llm():
    """llm/ 下任何 .py 都不能 import alphasolve.solver 或 alphasolve.agent。"""
    violations: list[tuple[Path, str]] = []
    for py_file in _iter_py_files("llm"):
        for imp in _collect_imports(py_file):
            if imp == "alphasolve.solver" or imp.startswith("alphasolve.solver."):
                violations.append((py_file, imp))
            if imp == "alphasolve.agent" or imp.startswith("alphasolve.agent."):
                violations.append((py_file, imp))
    assert not violations, (
        "llm/ must not import alphasolve.{solver,agent}.*; violations:\n"
        + "\n".join(f"  {p.relative_to(PACKAGE_ROOT)} imports {imp}" for p, imp in violations)
    )


def test_agent_does_not_import_llm_providers():
    """agent/ 下任何 .py 都不能 import alphasolve.llm.providers.* 或 alphasolve.llm.config.*。

    A 阶段已建立的不变式：agent 只看 llm 的公共 API（types），不碰 provider 实现。
    """
    violations: list[tuple[Path, str]] = []
    for py_file in _iter_py_files("agent"):
        for imp in _collect_imports(py_file):
            if imp.startswith("alphasolve.llm.providers") or imp.startswith("alphasolve.llm.config"):
                violations.append((py_file, imp))
    assert not violations, (
        "agent/ must not import alphasolve.llm.{providers,config}.*; violations:\n"
        + "\n".join(f"  {p.relative_to(PACKAGE_ROOT)} imports {imp}" for p, imp in violations)
    )
