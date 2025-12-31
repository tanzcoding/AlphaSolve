from typing import Dict, List, Tuple, Optional
import json
from openai import OpenAI
from wolframclient.evaluation import WolframLanguageSession
from .tools import *
from utils.logger import log_print


def _console_print(message: str, *, end: str = "\n"):
    """Print to console only (no file logging).

    We intentionally avoid `utils.logger.log_print()` here because `log_print()`
    also writes to the log file when `end == "\n"`. For streaming output we
    print fragments (`end == ""`) and later write the *full* buffers to the log.
    Mixing both causes log files to look out-of-order (e.g. "最终回答" divider
    appears before the buffered reasoning/answer blocks).
    """
    print(message, end=end, flush=True)

class LLMClient:
    def __init__(self, config: Dict, print_to_console: bool = False):
        """
        初始化 LLM 客户端
        
        Args:
            config: 包含供应商配置的字典，包括 base_url, api_key, model, tools 等
            print_to_console: 是否将流式输出打印到控制台（默认False）
        """
        self.config = config
        self.print_to_console = print_to_console

        def _resolve(v):
            return v() if callable(v) else v

        self.base_url = _resolve(config.get('base_url'))
        self.api_key = _resolve(config.get('api_key'))
        self.model = _resolve(config.get('model'))
        self.timeout = _resolve(config.get('timeout', 3600))
        self.temperature = _resolve(config.get('temperature', 1.0))
        self._static_params = _resolve(config.get('params', {})) or {}
        self.tools = _resolve(config.get('tools', None))  # 从配置读取工具列表
        
        # 初始化 OpenAI 客户端
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout
        )
    
    def _get_model_params(self) -> Dict:
        """直接从配置中读取参数，统一在 params 中定义"思考/推理"相关开关"""
        params: Dict = {
            "model": self.model,
            "temperature": self.temperature,
        }
        # 合并静态 params（包含 extra_body / reasoning_effort 等供应商差异化键）
        params.update(self._static_params)
        return params
    
    def get_result(self, messages: List[Dict], tools: Optional[List[Dict]] = None, print_to_console: Optional[bool] = None) -> Tuple[str, str]:
        """
        统一的获取 LLM 回复方法，始终使用流式输出
        
        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            tools: 工具列表（可选）。如果为None，使用self.tools；如果明确传入[]，则不使用工具
            print_to_console: 是否打印到控制台（可选）。如果为None，使用self.print_to_console
            
        Returns:
            Tuple[str, str]: (answer_content, reasoning_content)
        """
        # 确定是否打印到控制台
        _print_to_console = self.print_to_console if print_to_console is None else print_to_console
        
        # 确定使用的工具列表
        if tools is None:
            tools = self.tools
        
        # 如果没有工具，使用简单模式
        if not tools:
            return self._stream_simple(messages, _print_to_console)
        else:
            return self._stream_with_tools(messages, tools, _print_to_console)
    
    def _stream_simple(self, messages: List[Dict], print_to_console: bool) -> Tuple[str, str]:
        """
        无工具的流式响应
        
        Returns:
            Tuple[str, str]: (answer_content, reasoning_content)
        """
        model_params = self._get_model_params()
        extra_body = model_params.pop("extra_body", None)
        
        # 创建流式请求
        completion = self.client.chat.completions.create(
            messages=messages,
            stream=True,
            **model_params,
            **({"extra_body": extra_body} if extra_body else {})
        )

        reasoning_content = ""
        answer_content = ""
        is_answering = False

        if print_to_console:
            _console_print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                _console_print("此模型不返回思维链内容，仅打印最终回答\n")
            
            for chunk in completion:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                    if not is_answering:
                        _console_print(delta.reasoning_content, end="")
                    reasoning_content += delta.reasoning_content
                
                if hasattr(delta, "content") and delta.content:
                    if not is_answering:
                        _console_print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                        is_answering = True
                    _console_print(delta.content, end="")
                    answer_content += delta.content
        else:
            # 不打印时只收集内容
            for chunk in completion:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
                    reasoning_content += delta.reasoning_content
                if hasattr(delta, "content") and delta.content:
                    answer_content += delta.content

        # 流式输出完成后，将完整内容记录到日志
        from utils.logger import get_logger
        logger = get_logger(print_to_console=False)
        if reasoning_content:
            logger.info(f"[思维链内容]\n{reasoning_content}")
        if answer_content:
            logger.info(f"[最终回答]\n{answer_content}")
        
        return answer_content, reasoning_content
    
    def _stream_with_tools(self, messages: List[Dict], tools: List[Dict], print_to_console: bool) -> Tuple[str, str]:
        """
        带工具的流式响应，支持多轮工具调用
        
        Returns:
            Tuple[str, str]: (answer_content, reasoning_content)
        """
        model_params = self._get_model_params()
        extra_body = model_params.pop("extra_body", None)

        reasoning_content = ""
        answer_content = ""
        
        # 初始化工具执行环境
        tool_context = self._init_tool_context(tools, print_to_console)
        
        if print_to_console:
            _console_print("\n" + "=" * 20 + "思维链内容" + "=" * 20 + "\n")
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                _console_print("此模型不返回思维链内容，以下仅显示模型可能给出的 reasoning_content 与最终回答\n")
        
        # 多轮对话直至没有工具调用
        max_iterations = 20
        try:
            for _ in range(max_iterations):
                rc_parts = []
                ct_parts = []
                tool_calls_acc = []
                is_answering = False

                stream = self.client.chat.completions.create(
                    messages=messages,
                    tools=tools,
                    tool_choice='auto',
                    stream=True,
                    **model_params,
                    **({"extra_body": extra_body} if extra_body else {})
                )

                # 处理流式响应
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

                # 流式读取
                if print_to_console:
                    for chunk in stream:
                        delta = chunk.choices[0].delta

                        rc_part = getattr(delta, 'reasoning_content', None)
                        if rc_part:
                            if not is_answering:
                                _console_print(rc_part, end="")
                            rc_parts.append(rc_part)

                        ct_part = getattr(delta, 'content', None)
                        if ct_part:
                            if not is_answering:
                                _console_print("\n" + "=" * 20 + "最终回答" + "=" * 20 + "\n")
                                is_answering = True
                            _console_print(ct_part, end="")
                            ct_parts.append(ct_part)

                        tc_delta = getattr(delta, 'tool_calls', None)
                        if tc_delta:
                            process_tool_calls(tc_delta)
                else:
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

                # 合并本轮内容
                rc_buf = ''.join(rc_parts)
                content_buf = ''.join(ct_parts)

                if rc_buf:
                    reasoning_content += rc_buf
                if content_buf:
                    answer_content += content_buf
                
                # 记录本轮完整内容到日志（流式输出时每个片段用end=""不记录）
                from utils.logger import get_logger
                logger = get_logger(print_to_console=False)
                if rc_buf:
                    logger.info(f"[本轮思维链]\n{rc_buf}")
                if content_buf:
                    logger.info(f"[本轮回答]\n{content_buf}")

                # 添加assistant消息
                assistant_entry = {
                    'role': 'assistant',
                    'content': content_buf or '',
                    'reasoning_content': rc_buf or '',
                }
                if tool_calls_acc:
                    assistant_entry['tool_calls'] = tool_calls_acc
                messages.append(assistant_entry)

                # 如果没有工具调用则结束
                if not tool_calls_acc:
                    break

                # 处理工具调用
                if print_to_console:
                    _console_print("\n" + "-" * 10 + "[思维链中工具调用]" + "-" * 10)
                
                for tc in tool_calls_acc:
                    name = tc['function']['name']
                    args = json.loads(tc['function']['arguments'] or '{}')
                    
                    # 执行工具
                    tool_content, log_parts = self._execute_tool(name, args, tool_context, print_to_console)
                    
                    # 追加工具结果到reasoning_content
                    reasoning_content += ("\n".join(log_parts) + "\n")
                    
                    # 追加tool结果到消息列表
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id'),
                        'name': name,
                        'content': tool_content,
                    })

                # 提示继续思考
                if print_to_console:
                    _console_print("\n" + "-" * 10 + "[继续思考]" + "-" * 10)
        
        finally:
            # 清理工具上下文
            self._cleanup_tool_context(tool_context, print_to_console)

        return answer_content, reasoning_content
    
    def _init_tool_context(self, tools: List[Dict], print_to_console: bool) -> Dict:
        """初始化工具执行上下文"""
        context = {
            'python_env': {},
            'wolfram_session': None
        }
        
        # 检查是否需要Wolfram session
        needs_wolfram = any(tool.get('function', {}).get('name') == 'run_wolfram' for tool in tools)
        if needs_wolfram:
            if print_to_console:
                _console_print("[正在启动 Wolfram Language Session...]\n")
            try:
                context['wolfram_session'] = WolframLanguageSession()
                if print_to_console:
                    _console_print("[Wolfram Language Session 已启动]\n")
            except Exception as e:
                if print_to_console:
                    _console_print(f"[警告] Wolfram session 启动失败: {e}\n")
        
        return context
    
    def _cleanup_tool_context(self, context: Dict, print_to_console: bool):
        """清理工具执行上下文"""
        if context.get('wolfram_session'):
            try:
                context['wolfram_session'].terminate()
                if print_to_console:
                    _console_print("\n[Wolfram Language Session Terminated]")
            except Exception as e:
                if print_to_console:
                    _console_print(f"\n[Warning] Error terminating Wolfram session: {e}")
    
    def _execute_tool(self, name: str, args: Dict, context: Dict, print_to_console: bool) -> Tuple[str, List[str]]:
        """
        执行工具调用
        
        Returns:
            Tuple[str, List[str]]: (tool_content_json, log_parts)
        """
        log_parts = [f"\n[Tool Call] {name}"]
        
        if name == 'run_python':
            code = args.get('code', '')
            if print_to_console:
                _console_print(f"[Tool Call] run_python\nCode:\n{code}")
            
            log_parts.append(f"Code:\n{code}")
            stdout, error = run_python(code, context['python_env'], timeout_seconds=300)
            
            if print_to_console:
                if stdout:
                    _console_print(f"[stdout]\n{stdout}")
                if error:
                    _console_print(f"[error]\n{error}")
            
            if stdout:
                log_parts.append(f"[stdout]\n{stdout}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'stdout': stdout, 'error': error}, ensure_ascii=False)
        
        elif name == 'run_wolfram':
            code = args.get('code', '')
            if print_to_console:
                _console_print(f"[Tool Call] run_wolfram\nCode:\n{code}")
            
            log_parts.append(f"Code:\n{code}")
            
            if context['wolfram_session'] is None:
                error = "Wolfram session not available"
                output = ""
            else:
                output, error = run_wolfram(code, context['wolfram_session'])
            
            if print_to_console:
                if output:
                    _console_print(f"[output]\n{output}")
                if error:
                    _console_print(f"[error]\n{error}")
            
            if output:
                log_parts.append(f"[output]\n{output}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'output': output, 'error': error}, ensure_ascii=False)
        
        elif name == 'math_research_subagent':
            task_description = args.get('task_description', '')
            if print_to_console:
                _console_print(f"[Tool Call] math_research_subagent\nTask:\n{task_description}")
            
            log_parts.append(f"Task:\n{task_description}")
            result, error = run_subagent(task_description, print_to_console)
            
            if print_to_console:
                if result:
                    _console_print(f"[result]\n{result}")
                if error:
                    _console_print(f"[error]\n{error}")
            
            if result:
                log_parts.append(f"[result]\n{result}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'result': result, 'error': error}, ensure_ascii=False)
        
        elif name == 'solver_format_guard':
            candidate_response = args.get('candidate_response', '')
            if print_to_console:
                _console_print(f"[Tool Call] solver_format_guard\nCandidate response length: {len(candidate_response)}")
            
            if candidate_response:
                log_parts.append(f"Candidate response length: {len(candidate_response)}")
            
            result, error = solver_format_guard(candidate_response)
            
            if print_to_console:
                if result:
                    _console_print(f"[result]\n{result}")
                if error:
                    _console_print(f"[error]\n{error}")
            
            if result:
                log_parts.append(f"[result]\n{result}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'result': result, 'error': error}, ensure_ascii=False)
        
        else:
            if print_to_console:
                _console_print(f"[Tool Call] {name} (not implemented)")
            log_parts.append("(not implemented)")
            tool_content = json.dumps({'error': f'tool {name} not implemented in client'}, ensure_ascii=False)

        # Always write tool call + outputs to the log file as a single entry.
        # This keeps the log readable and avoids the "divider appears early" issue
        # caused by mixing per-fragment streaming logs with later buffered logs.
        try:
            from utils.logger import get_logger
            file_logger = get_logger(print_to_console=False)
            file_logger.info("[思维链中工具调用]\n" + "\n".join(log_parts).lstrip("\n"))
        except Exception:
            # Logging should not break the main pipeline.
            pass

        return tool_content, log_parts


# 使用示例
if __name__ == "__main__":
    import os
    CONFIG = {
        'base_url': 'https://api.moonshot.cn/v1',
        'api_key': lambda: os.getenv('MOONSHOT_API_KEY'),
        'model': 'kimi-k2-thinking',
        'timeout': 3600,
        'temperature': 1.0,
        'tools': [PYTHON_TOOL, WOLFRAM_TOOL]
    }
    log_print("测试:", print_to_console=True)
    llm = LLMClient(CONFIG, print_to_console=True)
    messages = [{"role": "user", "content": "1341*23412-1389=?"}]
    response = llm.get_result(messages)
    log_print(f"\n最终答案: {response[0]}", print_to_console=True)
