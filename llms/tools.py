import io
import sys
import traceback
import ast
import time
import builtins
import types
import importlib
import re
import json
from typing import Optional, Tuple
from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wlexpr


# NOTE:
# We intentionally block importing matplotlib (and pylab) inside `run_python()`.
# This is both to prevent plotting side-effects and to avoid heavy GUI/backends.
BANNED_IMPORT_ROOTS = {"matplotlib", "pylab"}


def _is_banned_module_name(module_name: str) -> bool:
    """Return True if module_name is a banned import root or submodule."""
    if not module_name:
        return False
    root = module_name.split(".", 1)[0]
    return root in BANNED_IMPORT_ROOTS


def _purge_banned_modules_from_sys_modules() -> None:
    """Best-effort removal of banned modules already imported in this process."""
    for name in list(sys.modules.keys()):
        if _is_banned_module_name(name):
            sys.modules.pop(name, None)


def run_python(code: str, env: dict = None, timeout_seconds: int = 300) -> Tuple[str, Optional[str]]:
    """
    类似Jupyter Notebook的 Python 执行器（演示用，无安全沙箱）

    执行给定的 Python 代码字符串，捕获 stdout 与错误信息。
    支持持久化环境（保持导入的包和变量），并自动输出最后一个表达式的值。

    Args:
        code: 要执行的Python代码
        env: 执行环境字典（用于保持会话状态）。如果为None，则创建新环境。
        timeout_seconds: 超时时间（秒）。超过该时间仍未返回则中止执行并返回 error="timeout"。
            注意：超时时会回滚 env 的键级别变更（新增/覆盖的变量会撤销）；
            但对已存在对象的原地修改（例如 list.append / dict.update）无法完全回滚。

    Returns:
        (stdout: str, error: str | None)
    """
    buf = io.StringIO()
    old_out = sys.stdout
    err = None

    # --- env 变更保护：用于超时/异常时回滚（键级别） ---
    env_snapshot = None
    env_keys_snapshot = None
    
    # 如果没有提供环境，创建新的
    if env is None:
        env = {}
    
    # --- 禁止导入 matplotlib/pylab（从根本上拦截 import / importlib.import_module） ---
    # 1) 清理进程级已加载的禁用模块，避免“之前已导入”被复用
    _purge_banned_modules_from_sys_modules()

    # 2) 清理 env 中已缓存的禁用模块对象（最佳努力）
    for k in list(env.keys()):
        v = env.get(k)
        if isinstance(v, types.ModuleType) and _is_banned_module_name(getattr(v, "__name__", "")):
            env.pop(k, None)

    # 3) 静态检查：显式 import matplotlib / from matplotlib ... 直接拒绝
    try:
        parsed_for_check = ast.parse(code, mode="exec")
        for node in ast.walk(parsed_for_check):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_banned_module_name(alias.name):
                        return "", "ImportError: matplotlib/pylab is disabled in this runtime"
            elif isinstance(node, ast.ImportFrom):
                if _is_banned_module_name(node.module or ""):
                    return "", "ImportError: matplotlib/pylab is disabled in this runtime"
    except SyntaxError:
        # 解析失败则跳过静态检查；运行期仍会被 import hook 拦截
        pass

    # 4) 运行期拦截：临时覆盖 builtins.__import__，并临时 monkey-patch importlib.import_module
    original_import = builtins.__import__

    def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        # name 可能是 'matplotlib' 或 'matplotlib.pyplot' 等
        if _is_banned_module_name(str(name)):
            raise ImportError("matplotlib/pylab is disabled in this runtime")
        return original_import(name, globals, locals, fromlist, level)

    # 确保环境中有内置函数，并注入我们的 __import__
    env_builtins = env.get("__builtins__")
    if isinstance(env_builtins, types.ModuleType):
        env_builtins = env_builtins.__dict__
    if env_builtins is None:
        env_builtins = builtins.__dict__
    # IMPORTANT: copy to avoid mutating global builtins dict
    env_builtins = dict(env_builtins)
    env_builtins["__import__"] = _blocked_import
    env["__builtins__"] = env_builtins

    original_importlib_import_module = getattr(importlib, "import_module", None)

    def _blocked_import_module(name, package=None):
        if _is_banned_module_name(str(name)):
            raise ImportError("matplotlib/pylab is disabled in this runtime")
        if original_importlib_import_module is None:
            raise ImportError("importlib.import_module is unavailable")
        return original_importlib_import_module(name, package=package)

    # 记录快照（浅拷贝）：用于在 timeout 时撤销本次执行对 env 的“键级别”修改
    env_snapshot = dict(env)
    env_keys_snapshot = set(env_snapshot.keys())

    # --- 软中断超时：使用 sys.settrace 在 Python 字节码级别检查时间 ---
    start_t = time.monotonic()
    old_trace = sys.gettrace()

    def _trace(frame, event, arg):
        # 只要还在执行 Python 代码，就会不断触发 trace 回调；到时抛出 TimeoutError 终止执行。
        if timeout_seconds is not None and timeout_seconds > 0:
            if (time.monotonic() - start_t) > timeout_seconds:
                raise TimeoutError("timeout")
        return _trace
    
    try:
        sys.stdout = buf
        sys.settrace(_trace)

        # 临时 patch builtins.__import__（避免通过 `import builtins; builtins.__import__(...)` 绕过）
        builtins.__import__ = _blocked_import

        # 临时 patch importlib.import_module（注意 finally 中恢复）
        if original_importlib_import_module is not None:
            importlib.import_module = _blocked_import_module
        
        # 解析代码，检测最后一个语句是否为表达式
        try:
            parsed = ast.parse(code, mode='exec')
            if parsed.body and isinstance(parsed.body[-1], ast.Expr):
                # 最后一个语句是表达式，需要自动输出
                # 将代码分为两部分：前面的语句 + 最后的表达式
                *statements, last_expr = parsed.body
                
                # 执行前面的语句
                if statements:
                    module_without_last = ast.Module(body=statements, type_ignores=[])
                    exec(compile(module_without_last, '<string>', 'exec'), env, env)
                
                # 评估并输出最后的表达式
                result = eval(compile(ast.Expression(body=last_expr.value), '<string>', 'eval'), env, env)
                if result is not None:
                    print(repr(result))
            else:
                # 没有表达式在最后，正常执行
                exec(code, env, env)
        except SyntaxError:
            # 如果解析失败，直接执行（可能是不完整的代码）
            exec(code, env, env)
             
    except TimeoutError:
        # 超时：回滚 env（键级别），并向上返回一个简洁的 "timeout"
        err = "timeout"
        # 回滚：删除新增键，恢复被覆盖的键
        for k in list(env.keys()):
            if env_keys_snapshot is not None and k not in env_keys_snapshot:
                env.pop(k, None)
        if env_snapshot is not None:
            for k, v in env_snapshot.items():
                env[k] = v
    except Exception:
        err = traceback.format_exc().strip()
    finally:
        sys.stdout = old_out
        # 恢复原 trace（避免影响外部逻辑）
        sys.settrace(old_trace)

        # 恢复 builtins.__import__（避免影响外部逻辑）
        builtins.__import__ = original_import

        # 恢复 importlib.import_module（避免影响外部逻辑）
        if original_importlib_import_module is not None:
            importlib.import_module = original_importlib_import_module
     
    return buf.getvalue(), err


def run_wolfram(code: str, session=None):
    """
    Wolfram 语言执行器
    
    执行给定的 Wolfram 语言代码字符串，捕获输出与错误信息。
    支持持久化 session（保持会话状态）。
    
    Args:
        code: 要执行的 Wolfram 语言代码
        session: WolframLanguageSession 实例（用于保持会话状态）。如果为 None，则创建新会话。
    
    Returns:
        (output: str, error: str | None)
    """
    output = ""
    err = None
    
    try:
        # 如果没有提供 session，session 应该由调用者管理
        # 这里假设 session 一定会被传入（由 get_result_with_tools 管理）
        if session is None:
            raise ValueError("Wolfram session must be provided by the caller")
        
        # 执行 Wolfram 代码
        result = session.evaluate(wlexpr(code))
        
        # 将结果转换为字符串
        output = str(result)
        
    except Exception as e:
        err = traceback.format_exc().strip()
    
    return output, err


def run_subagent(task_description: str, print_to_console: bool) -> Tuple[str, Optional[str]]:
    """
    数学研究子代理执行器
    
    独立执行小到中等规模的数学研究任务，使用 ReAct 风格工作流。
    可自主调用 Python 和 Wolfram Language 工具进行符号计算、数值分析等。
    
    Args:
        task_description: 清晰简洁的数学研究任务描述
    
    Returns:
        (result: str, error: str | None)
    """
    result = ""
    err = None
    
    try:
        # 动态导入以避免循环依赖
        from .utils import LLMClient
        from config.agent_config import AlphaSolveConfig
        
        # 使用 SUBAGENT_CONFIG 作为子代理配置
        config = AlphaSolveConfig.SUBAGENT_CONFIG
        client = LLMClient(config)
        
        # 构建子代理的系统提示
        system_prompt = """You are a specialized mathematical research sub-agent. Your task is to solve the given mathematical problem independently.

You have access to:
- run_python: Execute Python code with sympy, numpy, scipy, and other scientific libraries
- run_wolfram: Execute Wolfram Language for symbolic mathematics and advanced computations

Approach:
1. Understand the problem clearly
2. Break it down into logical steps
3. Use computational tools when needed
4. Provide a clear, concise final answer

Be thorough but efficient. Focus on delivering the correct result."""
        experience = """<experiences>
[Experience 1] For symbolic math, try BOTH SymPy (run_python) and Wolfram Language (run_wolfram) if the first attempt is inconclusive. Many tasks have complementary strengths: SymPy is great for quick algebraic manipulation and programmable workflows; Wolfram is often more robust for hard symbolic transforms.

[Experience 2] For indefinite integrals (symbolic antiderivatives), Wolfram Language (Integrate) is often more reliable than SymPy for difficult expressions. If SymPy returns an unevaluated Integral / cannot find a closed form, switch to Wolfram; also consider reporting conditions/assumptions (e.g., parameter ranges) that make a closed form possible.

[Experience 3] When simplifying expressions, always state assumptions. In SymPy, use symbols(..., positive=True/real=True) and simplify/together/factor/cancel; in Wolfram, prefer FullSimplify[..., Assumptions -> ...]. Many “different-looking” results are equivalent only under assumptions.

[Experience 4] For solving equations/inequalities with parameters, prefer Wolfram's Reduce for full condition sets. SymPy's solve can miss branches; use solveset or reduce_inequalities when appropriate, and verify solutions by substitution.

[Experience 5] For differential equations: try SymPy dsolve for simple ODEs; for harder ODE/PDE or when you need piecewise/parameter conditions, use Wolfram DSolve/NDSolve. Always verify by differentiating and substituting back.

[Experience 6] For numeric verification, increase precision to avoid false negatives (e.g., SymPy evalf(n=50), mpmath.mp.dps=50; Wolfram WorkingPrecision -> 50). Check multiple random points and edge cases (singularities, boundaries, large magnitude).

[Experience 7] Watch for branch cuts (Log, Power, Sqrt, inverse trig). If results disagree, test on representative domains and explicitly choose principal branches; present domain restrictions in the final explanation.
</experiences>"""
        
        messages = [
            {"role": "system", "content": system_prompt+"\n\n"+experience},
            {"role": "user", "content": task_description}
        ]
        
        # 子代理可以使用 Python 和 Wolfram 工具
        subagent_tools = [PYTHON_TOOL, WOLFRAM_TOOL]
        
        # 调用 get_result_with_tools 执行子代理任务（不打印到控制台）
        result, reasoning = client.get_result_with_tools(messages, subagent_tools, print_to_console=print_to_console)
        
    except Exception:
        err = traceback.format_exc().strip()
    
    return result, err


# ===== Solver 格式提醒工具（面向 LLM function-calling） =====
# 来自 prompts/solver.md 的关键约束：
# - 输出必须以 \begin{conjecture} 或 \begin{final_proof} 起手（允许前置空白），不能有任何其他前置内容
# - 仅允许两种整体结构（并建议无额外尾随内容）：
#   A) conjecture + proof + dependency
#   B) final_proof + dependency
# - dependency 环境内必须是 JSON array（例如 [] 或 [0,3,4]）

_SOLVER_CONJ_FULL_RE = re.compile(
    r"^\s*\\begin\{conjecture\}.*?\\end\{conjecture\}\s*"
    r"\\begin\{proof\}.*?\\end\{proof\}\s*"
    r"\\begin\{dependency\}.*?\\end\{dependency\}\s*$",
    re.DOTALL,
)
_SOLVER_FINAL_FULL_RE = re.compile(
    r"^\s*\\begin\{final_proof\}.*?\\end\{final_proof\}\s*"
    r"\\begin\{dependency\}.*?\\end\{dependency\}\s*$",
    re.DOTALL,
)
_SOLVER_DEP_RE = re.compile(r"\\begin\{dependency\}(.*?)\\end\{dependency\}", re.DOTALL)


def solver_format_guard(candidate_response: str = "") -> Tuple[str, Optional[str]]:
    """给 solver 输出提供格式提醒与校验。

    - 若 candidate_response 为空：返回格式提醒（mode="reminder"）。
    - 若 candidate_response 非空：严格校验整体结构与 dependency JSON（mode="check"）。

    Returns:
        (result_json_str, error_str|None)
    """
    try:
        expected_format = (
            "Your response must start with \\begin{conjecture} or \\begin{final_proof} (no preface). "
            "Allowed structures (and no extra text outside these environments):\n"
            "1) \\begin{conjecture}...\\end{conjecture}\n"
            "   \\begin{proof}...\\end{proof}\n"
            "   \\begin{dependency}[...]\\end{dependency}\n"
            "2) \\begin{final_proof}...\\end{final_proof}\n"
            "   \\begin{dependency}[...]\\end{dependency}\n"
            "Inside dependency must be a JSON array like [] or [0, 3, 4]."
        )

        text = candidate_response or ""
        if not text.strip():
            payload = {
                "ok": True,
                "mode": "reminder",
                "expected_format": expected_format,
            }
            return json.dumps(payload, ensure_ascii=False), None

        issues = []
        stripped = text.lstrip()
        if not (stripped.startswith("\\begin{conjecture}") or stripped.startswith("\\begin{final_proof}")):
            issues.append(
                "Response must start with \\begin{conjecture} or \\begin{final_proof} (no preface content)."
            )

        matches_final = bool(_SOLVER_FINAL_FULL_RE.match(text))
        matches_conj = bool(_SOLVER_CONJ_FULL_RE.match(text))
        if not (matches_final or matches_conj):
            issues.append(
                "Overall structure invalid. It must be exactly either (conjecture+proof+dependency) or (final_proof+dependency), "
                "with no extra content outside these environments."
            )

        dep_ids = None
        dep_match = _SOLVER_DEP_RE.search(text)
        if dep_match is None:
            issues.append("Missing \\begin{dependency}...\\end{dependency} block.")
        else:
            dep_raw = (dep_match.group(1) or "").strip()
            try:
                dep_ids = json.loads(dep_raw) if dep_raw else []
                if not isinstance(dep_ids, list):
                    issues.append("Dependency content must be a JSON array, e.g. [] or [0, 3, 4].")
                    dep_ids = None
            except Exception:
                issues.append(
                    "Dependency content is not valid JSON. It must be a JSON array like [] or [0, 3, 4]."
                )

        payload = {
            "ok": len(issues) == 0,
            "mode": "check",
            "issues": issues,
            "dependency": dep_ids,
            "expected_format": expected_format,
        }
        return json.dumps(payload, ensure_ascii=False), None
    except Exception:
        return "", traceback.format_exc().strip()


# ===== 定义工具函数规范 =====
# 定义一个工具列表，告诉AI模型它可以使用的工具
PYTHON_TOOL = {
    'type': 'function',
    'function': {
        'name': 'run_python',
        'description': "Execute Python code in an interactive environment similar to Jupyter Notebook. Key features: 1) Variables and imports persist across multiple code executions in the SAME conversation - you don't need to re-import libraries or re-define variables. 2) The last expression in your code will be automatically displayed (like Jupyter) - you can omit print() for the final result. 3) Use print() for intermediate outputs or multiple values. 4) Supports sympy, numpy, scipy, math, itertools, functools, and other standard libraries. Perfect for step-by-step mathematical computations and data analysis. IMPORTANT: Execution has a hard time limit (~5 minutes). If time limit is exceeded, the tool returns error=\"timeout\" and the environment changes from that execution are rolled back. SECURITY: Importing matplotlib/pylab is blocked in this runtime.",
        'parameters': {
            'type': 'object',
            'properties': {
                'code': {
                    'type': 'string',
                    'description': 'Python code to execute. Variables and imports from previous executions are available.'
                }
            },
            'required': ['code']
        }
    }
}
WOLFRAM_TOOL = {
    'type': 'function',
    'function': {
        'name': 'run_wolfram',
        'description': "Execute Wolfram Language code for advanced symbolic mathematics, calculus, differential equations, and algebraic computations. Key features: 1) Session persists across multiple code executions in the SAME conversation - defined variables and functions remain available. 2) Automatically returns the evaluation result - no need for explicit Print[]. 3) Ideal for: symbolic integration (Integrate), solving differential equations (DSolve, NDSolve), algebraic manipulation (Simplify, Factor, Expand), solving equations (Solve, Reduce), limits (Limit), series expansions (Series), and other advanced math operations. 4) Use Wolfram Language syntax: functions are capitalized (Sin, Cos, Log), use square brackets for function calls (Sin[x]), and use == for equations. Examples: 'Integrate[x^2 Sin[x], x]', 'DSolve[y''[x] + y[x] == 0, y[x], x]', 'Solve[x^2 - 5x + 6 == 0, x]'. Highly recommended for symbolic tasks.",
        'parameters': {
            'type': 'object',
            'properties': {
                'code': {
                    'type': 'string',
                    'description': 'Wolfram Language code to execute. Use Wolfram syntax (e.g., Sin[x], Integrate[expr, x]). Previous definitions remain available.'
                }
            },
            'required': ['code']
        }
    }
}
RESEARCH_SUBAGENT_DESCRIPTION = """A specialized autonomous sub-agent for detailed mathematical computations and verifications. This tool is your computational workhorse — DELEGATE *small, concrete sub-tasks* to it instead of doing them yourself.

**CRITICAL (scope): Do NOT hand the entire original problem to the sub-agent.**
You MUST first decompose the work and delegate only a *well-scoped* piece (one computation / one check / one derivation). Keep each delegation focused, bounded, and verifiable.

**CRITICAL (division of labor): YOU do high-level reasoning; the sub-agent does the math.**
- You (the main agent) should focus on: proposing approaches, choosing lemmas, deciding what to compute, and interpreting/organizing results.
- The sub-agent should focus on: actually computing/simplifying/solving/verifying with Python/Wolfram.
- Avoid doing step-by-step algebra/calculus manually in the main response. If you find yourself about to “work it out”, STOP and delegate that concrete computation.

Examples of good main-agent text:
- “I will try transforming the ODE into standard form and ask the sub-agent to solve it.”
- “I’ll ask the sub-agent to compute this integral and then I’ll analyze the parameter conditions.”
- “Let’s test the conjectured identity numerically at random points and then attempt symbolic simplification.”
Examples of bad main-agent behavior:
- Expanding and simplifying long expressions by hand.
- Performing multi-line derivative/integral manipulations manually.
- Claiming an equality without tool-backed verification.

**CRITICAL (delegation style): Delegate small tasks early and often.**
The goal is to offload computational heavy lifting while you keep control of the overall strategy and the final integrated solution.

The sub-agent autonomously:
- Performs multi-step symbolic derivations, algebraic manipulations, and equation solving using Python (SymPy/NumPy/SciPy) and Wolfram Language
- Executes numerical computations, optimizations, simulations, and high-precision calculations
- Verifies conjectures, checks edge cases, validates intermediate results, and explores counterexamples
- Conducts asymptotic analysis, solves differential equations, and derives intermediate formulas
- Returns detailed, verified results with working and justification

**What to delegate (small, concrete requests):**
- “Simplify this expression under these assumptions; return a canonical form.”
- “Compute/verify this integral/limit/series expansion; show steps.”
- “Solve this equation/ODE for these parameters; list solution branches.”
- “Numerically test this claim on these ranges; report counterexamples if any.”
- “Check edge cases (e.g., x→0, x→∞, parameter boundaries) for this lemma.”

**What NOT to delegate:**
- “Solve the whole problem.”
- “Figure out the entire approach/proof.”
- Dumping the full prompt without narrowing it to a specific computation or verification goal.

**Your role:** Do the high-level decomposition, choose *which* sub-questions to compute/verify, and integrate the results into the final reasoning. Use the sub-agent for concrete computations and checks, not as a replacement for end-to-end problem solving.
"""
RESEARCH_SUBAGENT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'math_research_subagent',
        'description': RESEARCH_SUBAGENT_DESCRIPTION,
        'parameters': {
            'type': 'object',
            'properties': {
                'task_description': {
                    'type': 'string',
                    'description': 'A clear and concise description of the mathematical research task to be solved by the sub-agent.'
                }
            },
            'required': ['task_description']
        }
    }
}


SOLVER_FORMAT_GUARD_TOOL = {
    'type': 'function',
    'function': {
        'name': 'solver_format_guard',
        'description': (
            "Format reminder + strict validator for solver outputs per prompts/solver.md. "
            "Call with no arguments to get a reminder; call with candidate_response to validate it."
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'candidate_response': {
                    'type': 'string',
                    'description': (
                        "(Optional) Draft response to validate. Must start with \\begin{conjecture} or \\begin{final_proof} "
                        "and contain only the allowed environments."
                    )
                }
            },
            'required': []
        }
    }
}


# Backward-compatible default tools for agents that only need the subagent.
TOOLS = [RESEARCH_SUBAGENT_TOOL]

# Tools intended for the solver agent.
SOLVER_TOOLS = [RESEARCH_SUBAGENT_TOOL, SOLVER_FORMAT_GUARD_TOOL]
