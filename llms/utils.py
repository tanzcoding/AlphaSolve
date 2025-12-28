from typing import Dict, List, Tuple
import json
from openai import OpenAI
from wolframclient.evaluation import WolframLanguageSession
#from config.agent_config import DEEPSEEK_CONFIG, MOONSHOT_CONFIG, VOLCANO_CONFIG, OPENROUTER_CONFIG, CUSTOM_LLM_CONFIG
from .tools import *

class LLMClient:
    def __init__(self, config: Dict):
        """
        初始化 LLM 客户端
        
        Args:
            config: 包含供应商配置的字典，包括 base_url, api_key, model 等
        """
        self.config = config

        def _resolve(v):
            return v() if callable(v) else v

        self.base_url = _resolve(config.get('base_url'))
        self.api_key = _resolve(config.get('api_key'))
        self.model = _resolve(config.get('model'))
        self.timeout = _resolve(config.get('timeout', 3600))
        self.temperature = _resolve(config.get('temperature', 1.0))
        self._static_params = _resolve(config.get('params', {})) or {}
        
        # 初始化 OpenAI 客户端
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )
    
    def _get_model_params(self) -> Dict:
        """直接从配置中读取参数，统一在 params 中定义“思考/推理”相关开关"""
        params: Dict = {
            "model": self.model,
            "temperature": self.temperature,
        }
        # 合并静态 params（包含 extra_body / reasoning_effort 等供应商差异化键）
        params.update(self._static_params)
        return params
    
    def get_result(self, messages: List[Dict], print_to_console: bool = False) -> Tuple[str, str]:
        """
        获取 LLM 的回复，始终使用流式输出，以防止网络超时
        
        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            print_to_console: 是否将流式输出打印到控制台
            
        Returns:
            answer_content: 最终回答内容
            reasoning_content: 完整思考过程内容
        """
        model_params = self._get_model_params()
        
        # 处理特殊参数
        extra_body = model_params.pop("extra_body", None)
        
        # 创建流式请求
        completion = self.client.chat.completions.create(
            messages=messages,
            stream=True,
            **model_params,
            **({"extra_body": extra_body} if extra_body else {})
        )

        reasoning_content = ""  # 完整思考过程
        answer_content = ""  # 完整回复
        is_answering = False  # 是否进入回复阶段

        if print_to_console:
            print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                print("此模型不返回思维链内容，仅打印最终回答\n")
            for chunk in completion:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                    if not is_answering:
                        print(delta.reasoning_content, end="", flush=True)
                    reasoning_content += delta.reasoning_content
                
                # 收到content，开始进行回复
                if hasattr(delta, "content") and delta.content:
                    if not is_answering:
                        print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                        is_answering = True
                    print(delta.content, end="", flush=True)
                    answer_content += delta.content    
        else:
            print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            for chunk in completion:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                    if not is_answering:
                        print(delta.reasoning_content, end="", flush=True)
                    reasoning_content += delta.reasoning_content
                
                # 收到content，开始进行回复
                if hasattr(delta, "content") and delta.content:
                    if not is_answering:
                        print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                        is_answering = True
                    print(delta.content, end="", flush=True)
                    answer_content += delta.content

        return answer_content, reasoning_content

    def get_result_with_tools(self, messages: List[Dict], tools: List[Dict], print_to_console: bool = False) -> Tuple[str, str]:
        """
        基于思维链的多轮工具调用对话，返回 (reasoning_content, answer_content)

        - 将给定的工具列表传入模型，由模型在思维链中主动选择并调用
        - 内置 run_python(code) 和 run_wolfram(code) 工具执行器，提供类似Jupyter Notebook的交互环境
        - 支持多轮（多次）工具调用，直到模型给出最终答案（无 tool_calls）
        - 每次调用此函数会创建新的执行环境；同一次调用中的多轮工具调用共享环境（变量、导入等会保持）

        Args:
            messages: 对话历史
            tools: OpenAI tools 规范的工具列表（其中若包含 name=="run_python" 或 "run_wolfram" 则会由本地执行器处理）
            print_to_console: 是否在控制台打印思维链、工具调用与最终回答

        Returns:
            Tuple[str, str]: (reasoning_content, answer_content)
        """

        model_params = self._get_model_params()
        extra_body = model_params.pop("extra_body", None)

        # 结果累积
        reasoning_content = ""
        answer_content = ""
        
        # 为本次会话创建持久化的执行环境（类似Jupyter Notebook）
        python_env = {}
        wolfram_session = None
        
        if print_to_console:
            print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                print("此模型不返回思维链内容，以下仅显示模型可能给出的 reasoning_content 与最终回答\n")
        
        # 检查是否需要 Wolfram session
        needs_wolfram = any(tool.get('function', {}).get('name') == 'run_wolfram' for tool in tools)
        if needs_wolfram:
            if print_to_console:
                print("[正在启动 Wolfram Language Session...]\n")
            try:
                wolfram_session = WolframLanguageSession()
                if print_to_console:
                    print("[Wolfram Language Session 已启动]\n")
            except Exception as e:
                if print_to_console:
                    print(f"[警告] Wolfram session 启动失败: {e}\n")

        # 多轮对话直至没有工具调用（流式处理）
        max_iterations = 20
        try:
            for _ in range(max_iterations):
                # 本轮累计缓冲 - 使用列表收集，最后 join
                rc_parts = []
                ct_parts = []
                tool_calls_acc = []  # 以增量方式拼接 tool_calls
                is_answering = False

                stream = self.client.chat.completions.create(
                    messages=messages,
                    tools=tools,
                    tool_choice='auto',
                    stream=True,
                    **model_params,
                    **({"extra_body": extra_body} if extra_body else {})
                )

                # 定义 tool_calls 处理函数（复用代码）
                def process_tool_calls(tc_delta):
                    for i, tc in enumerate(tc_delta):
                        while len(tool_calls_acc) <= i:
                            tool_calls_acc.append({'id': None, 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        if getattr(tc, 'id', None):
                            tool_calls_acc[i]['id'] = tc.id
                        fn = getattr(tc, 'function', None)
                        if fn:
                            if getattr(fn, 'name', None):
                                tool_calls_acc[i]['function']['name'] = fn.name
                            if getattr(fn, 'arguments', None):
                                tool_calls_acc[i]['function']['arguments'] += fn.arguments

                # 根据 print_to_console 选择不同的处理逻辑（避免在循环中判断）
                if print_to_console:
                    for chunk in stream:
                        delta = chunk.choices[0].delta

                        rc_part = getattr(delta, 'reasoning_content', None)
                        if rc_part:
                            if not is_answering:
                                print(rc_part, end="", flush=True)
                            rc_parts.append(rc_part)

                        ct_part = getattr(delta, 'content', None)
                        if ct_part:
                            if not is_answering:
                                print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                                is_answering = True
                            print(ct_part, end="", flush=True)
                            ct_parts.append(ct_part)

                        tc_delta = getattr(delta, 'tool_calls', None)
                        if tc_delta:
                            process_tool_calls(tc_delta)
                else:
                    # 不打印时的简化逻辑 - 循环中无任何打印操作
                    for chunk in stream:
                        delta = chunk.choices[0].delta

                        rc_part = getattr(delta, 'reasoning_content', None)
                        if rc_part:
                            rc_parts.append(rc_part)

                        ct_part = getattr(delta, 'content', None)
                        if ct_part:
                            ct_parts.append(ct_part)

                        tc_delta = getattr(delta, 'tool_calls', None)
                        if tc_delta:
                            process_tool_calls(tc_delta)

                # 使用 join 合并字符串，高效得多
                rc_buf = ''.join(rc_parts)
                content_buf = ''.join(ct_parts)

                # 累积到总体
                if rc_buf:
                    reasoning_content += rc_buf
                if content_buf:
                    answer_content += content_buf

                # 形成 assistant 历史消息（包含 reasoning_content 与 tool_calls）
                assistant_entry = {
                    'role': 'assistant',
                    'content': content_buf or '',
                    'reasoning_content': rc_buf or '',
                }
                if tool_calls_acc:
                    assistant_entry['tool_calls'] = tool_calls_acc
                messages.append(assistant_entry)

                # 若没有工具调用则对话结束
                if not tool_calls_acc:
                    break

                # 处理工具调用
                if print_to_console:
                    print("\n" + "-" * 10 + "[思维链中工具调用]" + "-" * 10)
                for tc in tool_calls_acc:
                    name = tc['function']['name']
                    args = json.loads(tc['function']['arguments'] or '{}')

                    # 处理 run_python
                    if name == 'run_python':
                        code = args.get('code', '')
                        if print_to_console:
                            print(f"[Tool Call] run_python\nCode:\n{code}")
                        # 使用持久化环境执行代码
                        # 默认 5 分钟超时；若超时则 run_python 会返回 error=="timeout" 并回滚本次执行对 env 的键级别修改
                        stdout, error = run_python(code, python_env, timeout_seconds=300)
                        if print_to_console:
                            if stdout:
                                print(f"[stdout]\n{stdout}")
                            if error:
                                print(f"[error]\n{error}")
                        tool_content = json.dumps({'stdout': stdout, 'error': error}, ensure_ascii=False)

                        # 将本次工具调用的关键信息也纳入 reasoning_content
                        _log_parts = [f"\n[Tool Call] {name}", f"Code:\n{code}"]
                        if stdout:
                            _log_parts.append(f"[stdout]\n{stdout}")
                        if error:
                            _log_parts.append(f"[error]\n{error}")
                        reasoning_content += ("\n".join(_log_parts) + "\n")
                    
                    # 处理 run_wolfram
                    elif name == 'run_wolfram':
                        code = args.get('code', '')
                        if print_to_console:
                            print(f"[Tool Call] run_wolfram\nCode:\n{code}")
                        
                        if wolfram_session is None:
                            error = "Wolfram session not available"
                            output = ""
                        else:
                            output, error  = run_wolfram(code, wolfram_session)
                        if print_to_console:
                            if output:
                                print(f"[output]\n{output}")
                            if error:
                                print(f"[error]\n{error}")
                        tool_content = json.dumps({'output': output, 'error': error}, ensure_ascii=False)

                        # 将本次工具调用的关键信息也纳入 reasoning_content
                        _log_parts = [f"\n[Tool Call] {name}", f"Code:\n{code}"]
                        if output:
                            _log_parts.append(f"[output]\n{output}")
                        if error:
                            _log_parts.append(f"[error]\n{error}")
                        reasoning_content += ("\n".join(_log_parts) + "\n")
                    
                    else:
                        tool_content = json.dumps({'error': f'tool {name} not implemented in client'}, ensure_ascii=False)
                        if print_to_console:
                            print(f"[Tool Call] {name} (not implemented)")
                        # 也写入 reasoning_content 以便完整复盘
                        reasoning_content += f"[Tool Call] {name} (not implemented)\n"

                    # 追加 tool 结果到消息列表
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id'),
                        'name': name,
                        'content': tool_content,
                    })

                # 提示继续思考
                if print_to_console:
                    print("\n" + "-" * 10 + "[继续思考]" + "-" * 10)
        
        finally:
            # 清理 Wolfram session
            if wolfram_session is not None:
                try:
                    wolfram_session.terminate()
                    if print_to_console:
                        print("\n[Wolfram Language Session Terminated]")
                except Exception as e:
                    if print_to_console:
                        print(f"\n[Warning] Error terminating Wolfram session: {e}")

        return answer_content, reasoning_content


# get_result函数的使用示例
if __name__ == "__main__":
    import os
    CONFIG = {
        'base_url': 'https://api.moonshot.cn/v1',
        'api_key': lambda: os.getenv('MOONSHOT_API_KEY'),
        'model': 'kimi-k2-thinking',
        'timeout': 3600,
        'temperature': 1.0,
        }
    print("测试:")
    llm = LLMClient(CONFIG)
    messages = [{"role": "user", "content": "1341*23412-1389=?"}]
    response = llm.get_result_with_tools(messages, TOOLS, print_to_console=True)
    print(response[1])
    
