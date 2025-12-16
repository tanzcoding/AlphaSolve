import io
import sys
import traceback
import ast


def run_python(code: str, env: dict = None):
    """
    类似Jupyter Notebook的 Python 执行器（演示用，无安全沙箱）

    执行给定的 Python 代码字符串，捕获 stdout 与错误信息。
    支持持久化环境（保持导入的包和变量），并自动输出最后一个表达式的值。

    Args:
        code: 要执行的Python代码
        env: 执行环境字典（用于保持会话状态）。如果为None，则创建新环境。

    Returns:
        (stdout: str, error: str | None)
    """
    buf = io.StringIO()
    old_out = sys.stdout
    err = None
    
    # 如果没有提供环境，创建新的
    if env is None:
        env = {}
    
    # 确保环境中有内置函数
    if '__builtins__' not in env:
        env['__builtins__'] = __builtins__
    
    try:
        sys.stdout = buf
        
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
            
    except Exception as e:
        err = traceback.format_exc().strip()
    finally:
        sys.stdout = old_out
    
    return buf.getvalue(), err

# ===== 定义工具函数规范 =====
# 定义一个工具列表，告诉AI模型它可以使用的工具
# 这里只定义了一个工具：run_python函数
TOOLS = [
    {
        'type': 'function',  # 工具类型为函数
        'function': {
            'name': 'run_python',  # 工具名称，与上面定义的函数名对应
            'description': "Execute Python code in an interactive environment similar to Jupyter Notebook. Key features: 1) Variables and imports persist across multiple code executions in the SAME conversation - you don't need to re-import libraries or re-define variables. 2) The last expression in your code will be automatically displayed (like Jupyter) - you can omit print() for the final result. 3) Use print() for intermediate outputs or multiple values. 4) Supports sympy, numpy, scipy, math, itertools, functools, and other standard libraries. Perfect for step-by-step mathematical computations and data analysis. Warning: Don't use matplotlib to plot ANYTHING!",
            # 工具描述，告诉模型这个工具的功能和使用方法
            # 强调类似Jupyter Notebook的交互式环境，变量和导入持久化，最后表达式自动输出
            
            'parameters': {
                'type': 'object',  # 参数类型为对象
                'properties': {
                    'code': {
                        'type': 'string',  # code参数的类型为字符串
                        'description': 'Python code to execute. Variables and imports from previous executions are available.'  # 参数描述
                    }
                },
                'required': ['code']  # 必需的参数列表
            }
        }
    }
]