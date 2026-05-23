"""agent/ui/render.py 和 solver/ui/team_renderer.py 共用的渲染 helper。

放在 agent/ 下而非顶层是因为：solver 允许 import agent，反过来不行。
目前仅含全局 Rich Console 实例（agent CLI 和 solver dashboard 都用它）。
"""
from __future__ import annotations

from rich.console import Console


RICH_CONSOLE = Console()
