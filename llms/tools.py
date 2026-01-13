import io
import sys
import traceback
import ast
import time
import threading
import queue
import builtins
import types
import importlib
import re
import json
import os
from typing import Optional, Tuple
from wolframclient.language import wlexpr
from utils.logger import Logger
from agents.shared_context import Lemma
from utils.utils import extract_substring, apply_unified_diff, search_and_replace
from agents.shared_context import SharedContext


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
                    # Use built-in print here since it's executed in the user's code context
                    # This should go to the captured stdout buffer
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


def run_wolfram(code: str, session=None, timeout_seconds: int = 300):
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
    if session is None:
        raise ValueError("Wolfram session must be provided by the caller")

    result_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue(maxsize=1)

    def _worker():
        try:
            result = session.evaluate(wlexpr(code))
            result_queue.put(("output", str(result)))
        except Exception:
            result_queue.put(("error", traceback.format_exc().strip()))

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        # 超时：终止 Wolfram session 并返回 timeout
        err = "timeout"
        try:
            session.terminate()
        except Exception as terminate_exc:
            err = f"timeout (failed to terminate session cleanly: {terminate_exc})"
        return "", err

    try:
        kind, payload = result_queue.get_nowait()
    except queue.Empty:
        return "", "unknown_error: wolfram worker produced no result"

    if kind == "output":
        return payload, None
    else:
        return "", payload


def run_subagent(task_description: str, logger: Logger, shared: SharedContext) -> Tuple[str, Optional[str]]:
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
    
    logger.log_print('entering subagent...', module='subagent')
    
    try:
        # 动态导入以避免循环依赖
        from .utils import LLMClient
        from config.agent_config import AlphaSolveConfig
        
        # 使用 SUBAGENT_CONFIG 作为子代理配置
        config = AlphaSolveConfig.SUBAGENT_CONFIG
        client = LLMClient(module='subagent', config=config, logger=logger)
        
        # 构建子代理的系统提示（回答会进入主 agent 上下文：尽量省 token，但数学推导必须完整展开）
        system_prompt = """You are a mathematical research sub-agent. Solve the given subtask correctly (compute/verify/derive).
Correctness is mandatory: clearly state assumptions; every result you provide must be mathematically sound and rigorously verified. 
Ask for clarifications if the task is ambiguous. Tell the main agent if the task is not self-contained or too large for you to handle.
Tools available: run_python (SymPy/NumPy/SciPy), run_wolfram (Wolfram Language).
OUTPUT RULES (token-efficient): plain text only (do NOT use markdown). Minimize blank lines, indentation, and extra spaces, but do NOT omit mathematical steps: fully expand derivations (no 'obvious', 'routine', 'it is easy'). Prefer compact dense formatting (e.g., short paragraphs; equations inline; optional section labels like Result/Assumptions/Proof/Checks).
If the subtask is too large: do NOT attempt to solve it beyond your capacity; state what you verified/failed + suggest a smaller, more manageable subtask that you can complete for the next step."""
        experience = """<experiences>
Use SymPy first; if inconclusive/hard symbolic, switch to Wolfram for powerful symbolic capability. Always include assumptions (domains/parameters). For param equations/inequalities prefer Reduce and verify branches by substitution. For numerics: increase precision; test random points + edge/singularity cases. Watch branch cuts (Log/Sqrt/Power).
</experiences>"""
        
        messages = [
            {"role": "system", "content": system_prompt+"\n\n"+experience},
            {"role": "user", "content": "<task_description>\n" + task_description + "\n</task_description>"},
        ]
        
        # 调用 get_result 执行子代理任务（工具已在配置中设置）
        result, reasoning,_ = client.get_result(messages=messages, shared=shared)
        
    except Exception:
        err = traceback.format_exc().strip()
    
    return result, err


# ===== Solver 格式提醒工具（面向 LLM function-calling） =====
# 来自 prompts/solver.md 的关键约束：
# - 输出必须以 <conjecture>起手，不能有任何其他前置内容
# - 用<conjecture>和</conjecture>包裹猜想内容
# - 若非最终证明，则紧接着用<proof>和</proof>包裹证明内容
# - 最终猜想则用<final_conjecture>和</final_conjecture>包裹猜想内容，并仍需跟随<proof>...</proof>
# - 输出必须包含 <dependency> 环境
# - 仅允许两种整体结构（并建议无额外尾随内容）：
#   A) conjecture + proof + dependency
#   B) final_conjecture + proof + dependency
# - dependency 环境内必须是 JSON array（例如 [] 或 [0,3,4]）

_SOLVER_CONJ_FULL_RE = re.compile(
    r"^\s*<conjecture>.*?</conjecture>\s*"
    r"<proof>.*?</proof>\s*"
    r"<dependency>.*?</dependency>\s*$",
    re.DOTALL,
)
_SOLVER_FINAL_FULL_RE = re.compile(
    r"^\s*<final_conjecture>.*?</final_conjecture>\s*"
    r"<proof>.*?</proof>\s*"
    r"<dependency>.*?</dependency>\s*$",
    re.DOTALL,
)
_SOLVER_DEP_RE = re.compile(r"<dependency>(.*?)</dependency>", re.DOTALL)


def solver_format_guard(candidate_response: str = "") -> Tuple[str, Optional[str]]:
    """给 solver 输出提供格式提醒与校验。

    - 若 candidate_response 为空：返回格式提醒（mode="reminder"）。
    - 若 candidate_response 非空：严格校验整体结构与 dependency JSON（mode="check"）。

    Returns:
        (result_json_str, error_str|None)
    """
    try:
        expected_format = (
            "Response must start immediately with either <conjecture> or <final_conjecture>. "
            "Only two structures are allowed, with no extra text outside them:\n"
            "1) <conjecture>...</conjecture>\n"
            "   <proof>...</proof>\n"
            "   <dependency>[...]</dependency>\n"
            "2) <final_conjecture>...</final_conjecture>\n"
            "   <proof>...</proof>\n"
            "   <dependency>[...]</dependency>\n"
            "Inside <dependency></dependency> you must place a JSON array like [] or [0, 3, 4]."
            "VERY IMPORTANT: when your conjecture is a complete solution to the problem, use <final_conjecture> instead of <conjecture>."
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
        starts_with_conj = stripped.startswith("<conjecture>")
        starts_with_final = stripped.startswith("<final_conjecture>")
        if not (starts_with_conj or starts_with_final):
            issues.append(
                "Response must start with <conjecture> or <final_conjecture>, with no preface content."
            )

        matches_final = bool(_SOLVER_FINAL_FULL_RE.match(text))
        matches_conj = bool(_SOLVER_CONJ_FULL_RE.match(text))
        if not (matches_final or matches_conj):
            issues.append(
                "Overall structure invalid. It must be exactly either (conjecture+proof+dependency) or (final_conjecture+proof+dependency), "
                "with no extra content outside these environments."
            )

        dep_ids = None
        dep_match = _SOLVER_DEP_RE.search(text)
        if dep_match is None:
            issues.append("Missing <dependency>...</dependency> block.")
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


def apply_new_statement_to_lemma(lemma: Lemma, new_statement: str) -> str:
    """Replace the lemma statement entirely with the provided text."""
    try:
        if not isinstance(new_statement, str) or not new_statement.strip():
            raise ValueError("new_statement must be a non-empty string")

        lemma.update({
            "statement": new_statement,
        })

        return "Conjecture statement updated successfully:\n" + f"<conjecture>\n{lemma.get("statement", "")}</conjecture>"
    except Exception as exc:
        return "[error]\n" + str(exc)


def apply_proof_anchor_edit(
    lemma: Lemma,
    begin_marker: str,
    end_marker: str,
    proof_replacement: str,
) -> str:
    """Replace the proof span between begin_marker and end_marker (inclusive)."""
    try:
        if not begin_marker.strip() or not end_marker.strip():
            raise ValueError("Both begin_marker and end_marker must be provided")
        if len(begin_marker) > 100 or len(end_marker) > 100:
            raise ValueError("Markers must be 100 characters or fewer")

        proof = lemma.get("proof", "")
        start_idx = proof.find(begin_marker)
        if start_idx == -1:
            raise ValueError("begin_marker not found in proof text")

        end_idx = proof.find(end_marker, start_idx + len(begin_marker))
        if end_idx == -1:
            raise ValueError("end_marker not found after begin_marker")

        end_idx += len(end_marker)
        new_proof = proof[:start_idx] + proof_replacement + proof[end_idx:]

        lemma.update({
            "proof": new_proof,
            "apply_diff_error": None,
        })

        return "Updated successfully:\n" + f"<conjecture>\n{lemma.get("statement", "")}</conjecture>\n<proof>{lemma.get("proof", "")}</proof>"
    except Exception as exc:
        return "[error]\n" + str(exc)

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
RESEARCH_SUBAGENT_DESCRIPTION = """Autonomous sub-agent for concrete math computations/verifications (symbolic or numeric) using tools like Python/Wolfram.

**CRITICAL:** Each call spawns a NEW sub-agent with NO memory of previous calls. Provide a self-contained task description every time.

**Scope:** Do NOT delegate the whole problem. Decompose and delegate one bounded task (one computation/check/derivation).

**Division of labor:** You do high-level strategy (plan, choose what method to use, and decide what to explore) and integrate results; the sub-agent does the computation.

**CRITICAL (when to use): Call subagent EARLY and very OFTEN.** If you are about to do any nontrivial calculation/verification (multi-line algebra, symbolic simplification, case splits, solving equations/ODEs, checking edge cases, numeric experiments), you MUST call the sub-agent instead of doing it manually. If you catch yourself “working it out”, STOP and delegate that concrete subtask.

**Reliability:** The sub-agent can be wrong—keep tasks verifiable and cross-check results by delegating the same or similar task to a second sub-agent if needed.

Good requests (small + concrete): simplify under assumptions; compute/verify an integral/limit/series; solve an equation/ODE with parameter cases; compute a Groebner basis / eliminate variables / check ideal membership; compute a fundamental group / homology in a toy case; check whether a map is continuous/smooth from explicit formulas; verify a coordinate chart transition; compute curvature/Christoffel symbols for a given metric; numeric testing/counterexamples; edge-case checks.
Bad requests: "solve the whole problem", "find the entire approach", dumping the full prompt.
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
                    'description': 'A clear, complete and **self-contained** description of the mathematical research task to be solved by the sub-agent.'
                }
            },
            'required': ['task_description']
        }
    }
}

SOLVER_FORMAT_GUARD_TOOL = {
    'type': 'function',
    'function': {
        'name': 'generate_conjecture_format_checker',
        'description': (
            "Call with no arguments to get a format reminder; call with candidate_response to validate its format. "
            "IMPORTANT: This tool does NOT generate a conjecture or lemma. It only validates format and does not consume lemma budget."
            ""
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'candidate_response': {
                    'type': 'string',
                    'description': (
                        "(Optional) Draft response to validate. If empty, the tool returns a format reminder. "
                        "and contain only the allowed environments."
                    )
                }
            },
            'required': []
        }
    }
}


MODIFY_STATEMENT_DESCRIPTION = """Replace the conjecture statement *in full*.

Guidelines:
- Put the complete replacement text in `new_statement`.
- Write the statement as a standalone, self-contained sentence (unless the required notions are already defined by dependent lemmas).
- Do **not** add a label prefix such as "Lemma:", "Conjecture:", or "Proposition:" (the surrounding UI already supplies the label).
  Examples:
    - Good: "For all real x, we have ..."
    - Bad: "Lemma: For all real x, we have ..."
    - OK: "(Classification of …) For all real x, we have ..."  (a brief parenthetical title/context is fine)
- Use Markdown and/or LaTeX when it improves mathematical clarity.
- You can call this tool multiple times; each call overwrites the entire statement.
"""


MODIFY_STATEMENT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'modify_statement',
        'description': MODIFY_STATEMENT_DESCRIPTION,
        'parameters': {
            'type': 'object',
            'properties': {
                'new_statement': {
                    'type': 'string',
                    'description': 'Complete replacement text for the conjecture statement.'
                }
            },
            'required': ['new_statement']
        }
    }
}


MODIFY_PROOF_DESCRIPTION = """Replace a span of the proof using short anchors.

Parameters:
- `begin_marker`: ≤100 characters that appear verbatim in the current proof and mark the inclusive start of the edit.
- `end_marker`: ≤100 characters that appear after the begin marker and mark the inclusive end of the edit.
- `proof_replacement`: Text that replaces the span from the first character of `begin_marker` through the last character of `end_marker` (anchors are removed unless reintroduced).

Guidelines:
- Choose anchors that are unique yet short (≤100 chars each).
- The replacement may be empty (deletion) or contain multiple paragraphs (insertion/rewrite).
- Call this tool as many times as necessary to stage complex edits.
"""


MODIFY_PROOF_TOOL = {
    'type': 'function',
    'function': {
        'name': 'modify_proof',
        'description': MODIFY_PROOF_DESCRIPTION,
        'parameters': {
            'type': 'object',
            'properties': {
                'begin_marker': {
                    'type': 'string',
                    'description': '≤100 character snippet marking the inclusive start of the span to replace. As short and unique as possible. Recommended ≤50 character.'
                },
                'end_marker': {
                    'type': 'string',
                    'description': '≤100 character snippet marking the inclusive end of the span to replace. As short and unique as possible. Recommended ≤50 character.'
                },
                'proof_replacement': {
                    'type': 'string',
                    'description': 'Replacement text that will take the place of the removed span.'
                }
            },
            'required': ['begin_marker', 'end_marker', 'proof_replacement']
        }
    }
}


READ_LEMMA_DESCRIPTION = """Read the full proof of an existing lemma by its id.

Use this tool when you want to read the full proof of an existing collected lemma in the background.

Parameters:
- lemma_id: integer index of the lemma (0-based).
"""

READ_LEMMA_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_lemma',
        'description': READ_LEMMA_DESCRIPTION,
        'parameters': {
            'type': 'object',
            'properties': {
                'lemma_id': {
                    'type': 'integer',
                    'description': '0-based lemma id to read.'
                }
            },
            'required': ['lemma_id']
        }
    }
}


READ_CURRENT_CONJECTURE_AGAIN_DESCRIPTION = """Read the statement and proof of the current conjecture yet to be refined.

Use this tool when you need to double check or reference the exact text of the current conjecture you are working on. NO parameter is needed.
"""

READ_CURRENT_CONJECTURE_AGAIN_TOOL = {
    'type': 'function',
    'function': {
        'name': 'read_current_conjecture_again',
        'description': READ_CURRENT_CONJECTURE_AGAIN_DESCRIPTION,
        'parameters': {
            'type': 'object',
            'properties': {
            },
            'required': []
        }
    }
}
