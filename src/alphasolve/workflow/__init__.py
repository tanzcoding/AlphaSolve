"""第三层：编排层。

依赖契约：
- 允许 import: alphasolve.agent.*（公共 API + 私有）
- 允许 import: alphasolve.llm.*（公共 API，纯类型/协议如 ChatClient/Message）
- 允许 import: alphasolve.execution.*、alphasolve.utils.*、alphasolve.config.*
- 禁止 import: alphasolve.llm.providers.*、alphasolve.llm.config.*（实现细节）

公共 API: AlphaSolve（入口类）。
"""
from .app import AlphaSolve, run_alphasolve

__all__ = [
    "AlphaSolve",
    "run_alphasolve",
]
