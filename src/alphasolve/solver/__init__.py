"""第三层：AlphaSolve 数学求解 app。

依赖契约（phase C 后）：
- 允许 import: alphasolve.agent.*（公共 API + 私有）
- 允许 import: alphasolve.llm.types（公共类型/协议）
- 允许 import: alphasolve.config.*（顶层配置：presets/profiles/PACKAGE_ROOT）
- 禁止 import: alphasolve.llm.providers.*、alphasolve.llm.config.*（实现细节）

包含子域：
- solver.app / solver.orchestrator / solver.worker / solver.curator —— 编排核心
- solver.subagent_service —— subagent 递归调用
- solver.client_factory / solver.demo / solver.project / solver.solution / solver.workspace_access
- solver.execution —— Python/Wolfram 执行 gateway + runners
- solver.logging —— Logger / EventLogWriter / LogSession
- solver.ui —— PropositionTeamRenderer / dashboard（多 agent 协同 UI）
- solver.wolfram_probe —— 启动时 Wolfram kernel 可用性探测
- solver.prompts/ —— 所有 agent role 的 system prompt markdown
- solver.config/ —— agent/subagent YAML + agents.yaml suite 入口

公共 API：AlphaSolve（入口类）+ run_alphasolve。
"""
from .app import AlphaSolve, run_alphasolve

__all__ = [
    "AlphaSolve",
    "run_alphasolve",
]
