import io
import sys
import traceback
import ast
import time
from typing import Optional, Tuple
from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wlexpr


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
    
    # 确保环境中有内置函数
    if '__builtins__' not in env:
        env['__builtins__'] = __builtins__

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


# ===== 定义工具函数规范 =====
# 定义一个工具列表，告诉AI模型它可以使用的工具
PYTHON_TOOL = {
    'type': 'function',
    'function': {
        'name': 'run_python',
        'description': "Execute Python code in an interactive environment similar to Jupyter Notebook. Key features: 1) Variables and imports persist across multiple code executions in the SAME conversation - you don't need to re-import libraries or re-define variables. 2) The last expression in your code will be automatically displayed (like Jupyter) - you can omit print() for the final result. 3) Use print() for intermediate outputs or multiple values. 4) Supports sympy, numpy, scipy, math, itertools, functools, and other standard libraries. Perfect for step-by-step mathematical computations and data analysis. IMPORTANT: Execution has a hard time limit (~5 minutes). If time limit is exceeded, the tool returns error=\"timeout\" and the environment changes from that execution are rolled back. Warning: Don't use matplotlib to plot ANYTHING!",
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
TOOLS = [PYTHON_TOOL]
