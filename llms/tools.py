import io
import sys
import traceback


def run_python(code: str):
    """
    简单的 Python 执行器（演示用，无安全沙箱）

    执行给定的 Python 代码字符串，捕获 stdout 与错误信息。

    Returns:
        (stdout: str, error: str | None)
    """
    buf = io.StringIO()
    old_out = sys.stdout
    err = None
    try:
        sys.stdout = buf
        env = {}
        exec(code, env, env)
    except Exception as e:
        err = "".join(traceback.format_exception_only(type(e), e)).strip()
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
            'description': "Execute Python codes; please manually print the computation result to stdout; import the needed libraries every time you use this function. Recommended libraries for complex math problems include sympy, numpy, scipy, math, itertools, functools, etc.",
            # 工具描述，告诉模型这个工具的功能和使用方法
            # 需要自行导入所需的库，并手动打印计算结果到stdout
            
            'parameters': {
                'type': 'object',  # 参数类型为对象
                'properties': {
                    'code': {
                        'type': 'string',  # code参数的类型为字符串
                        'description': 'Python code to execute'  # 参数描述
                    }
                },
                'required': ['code']  # 必需的参数列表
            }
        }
    }
]