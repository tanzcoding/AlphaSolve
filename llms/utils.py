from typing import Dict, List, Tuple, Optional
import json
from openai import OpenAI
from wolframclient.evaluation import WolframLanguageSession
from .tools import *

class LLMClient:
    def __init__(self, module: str, config: Dict, logger: Logger):
        """
        初始化 LLM 客户端
        
        Args:
            config: 包含供应商配置的字典，包括 base_url, api_key, model, tools 等
            print_to_console: 是否将流式输出打印到控制台（默认False）
        """
        self.module = module
        self.config = config
        self.logger = logger

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
    
    def get_result(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Tuple[str, str, List[Dict]]:
        """
        统一的获取 LLM 回复方法，始终使用流式输出
        
        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            tools: 工具列表（可选）。如果为None，使用self.tools；如果明确传入[]，则不使用工具
            
        Returns:
            Tuple[str, str, List[Dict]]: (answer_content, reasoning_content, updated_messages)
        """
        b = time.time()
        # 确定使用的工具列表
        if tools is None:
            tools = self.tools if self.tools else []
        
        logger = self.logger
        reasoning_content = ""
        answer_content = ""
        
        # 初始化工具执行环境
        tool_context = self._init_tool_context(tools) if tools else {}
        
        # 多轮对话直至没有工具调用
        max_iterations = 100
        try:
            for _ in range(max_iterations):
                # 获取一次响应
                message = self._get_one_response(messages, tools)
                
                # 将message添加到messages
                messages.append(message)
                
                # 累积reasoning_content和content
                if message.get('reasoning_content'):
                    reasoning_content += message['reasoning_content']
                if message.get('content'):
                    answer_content += message['content']
                
                # 如果没有工具调用则结束
                tool_calls = message.get('tool_calls', [])
                if not tool_calls:
                    break
                
                # 处理工具调用
                logger.log_print("-" * 10 + "[思维链中工具调用]" + "-" * 10)
                
                for tc in tool_calls:
                    name = tc['function']['name']
                    args = json.loads(tc['function']['arguments'] or '{}')
                    
                    # 执行工具
                    tool_content, log_parts = self._execute_tool(name, args, tool_context)
                    
                    # 追加工具结果到reasoning_content
                    reasoning_content += ("\n".join(log_parts) + "\n")
                    
                    # 追加tool结果到消息列表
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id'),
                        'name': name,
                        'content': tool_content,
                    })
        
        finally:
            # 清理工具上下文
            if tool_context:
                self._cleanup_tool_context(tool_context)

        logger.log_print(
            f"event=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer_content)} cot_len={len(reasoning_content)}",
            module=self.module,
        )

        return answer_content, reasoning_content, messages
        
    def _get_one_response(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        """
        获取一次响应，结束原因可能为工具调用或最终回答
        采用流式输出方式，但将结果拼接成类似非流式输出的message格式
        
        Args:
            messages: 消息列表
            tools: 工具列表
            
        Returns:
            Dict: 拼接好的assistant message，格式为 {"role": "assistant", "content": ..., "reasoning_content": ..., "tool_calls": [...]}
        """
        model_params = self._get_model_params()
        extra_body = model_params.pop("extra_body", None)
        logger = self.logger

        reasoning_parts = []
        content_parts = []
        tool_calls_acc = []
        is_answering = False

        # For audit/debug: record the exact messages sent to LLM.
        logger.log_print(
            "event=llm_messages\n" + json.dumps(messages, ensure_ascii=False, indent=2),
            module=self.module,
        )

        # 判断是第一次调用还是继续思考
        # 如果messages最后一条是tool角色，说明是继续思考
        is_continuation = len(messages) > 0 and messages[-1].get('role') == 'tool'
        
        if is_continuation:
            logger.log_print("-" * 10 + "[继续思考]" + "-" * 10)
        else:
            logger.log_print("=" * 20 + "思维链内容" + "=" * 20)
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                logger.log_print("此模型不返回思维链内容，以下仅显示模型可能给出的 reasoning_content 与最终回答\n")

        # 创建流式请求
        stream = self.client.chat.completions.create(
            messages=messages,
            tools=tools,
            tool_choice='auto' if tools else None,
            stream=True,
            **model_params,
            **({"extra_body": extra_body} if extra_body else {})
        )

        # 流式读取并拼接
        for chunk in stream:
            delta = chunk.choices[0].delta

            # 处理 reasoning_content
            rc_part = getattr(delta, 'reasoning_content', None)
            if rc_part:
                if not is_answering:
                    logger.log_print(rc_part, end="")
                reasoning_parts.append(rc_part)

            # 处理 content
            ct_part = getattr(delta, 'content', None)
            if ct_part:
                if not is_answering:
                    logger.log_print("=" * 20 + "最终回答" + "=" * 20 + "\n")
                    is_answering = True
                logger.log_print(ct_part, end="")
                content_parts.append(ct_part)

            # 处理 tool_calls
            tc_delta = getattr(delta, 'tool_calls', None)
            if tc_delta:
                self._process_tool_calls(tc_delta, tool_calls_acc)

        # 拼接成完整的message
        reasoning_content = ''.join(reasoning_parts)
        content = ''.join(content_parts)
        
        assistant_message = {
            'role': 'assistant',
            'content': content or '',
            'reasoning_content': reasoning_content or '',
        }
        
        if tool_calls_acc:
            assistant_message['tool_calls'] = tool_calls_acc
        
        return assistant_message

    def _process_tool_calls(self, tc_delta, tool_calls_acc):
        """将流式 tool_calls 片段聚合为完整调用"""
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

    def _init_tool_context(self, tools: List[Dict]) -> Dict:
        """初始化工具执行上下文"""
        context = {
            'python_env': {},
            'wolfram_session': None
        }
        
        # 检查是否需要Wolfram session
        needs_wolfram = any(tool.get('function', {}).get('name') == 'run_wolfram' for tool in tools)
        if needs_wolfram:
            if self.logger.print_to_console_default:
                print("[正在启动 Wolfram Language Session...]\n", end="")
            try:
                context['wolfram_session'] = WolframLanguageSession()
                if self.logger.print_to_console_default:
                    print("[Wolfram Language Session 已启动]\n", end="")
            except Exception as e:
                if self.logger.print_to_console_default:
                    print(f"[警告] Wolfram session 启动失败: {e}\n", end="")

        return context
    
    def _cleanup_tool_context(self, context: Dict):
        """清理工具执行上下文"""
        if context.get('wolfram_session'):
            try:
                context['wolfram_session'].terminate()
                if self.logger.print_to_console_default:
                    print("\n[Wolfram Language Session Terminated]", end="")
            except Exception as e:
                if self.logger.print_to_console_default:
                    print(f"\n[Warning] Error terminating Wolfram session: {e}", end="")
    
    def _execute_tool(self, name: str, args: Dict, context: Dict) -> Tuple[str, List[str]]:
        """
        执行工具调用
        
        Returns:
            Tuple[str, List[str]]: (tool_content_json, log_parts)
        """
        log_parts = [f"\n[Tool Call] {name}"]
        
        if name == 'run_python':
            code = args.get('code', '')
            if self.logger.print_to_console_default:
                print(f"[Tool Call] run_python\nCode:\n{code}")
            
            log_parts.append(f"Code:\n{code}")
            stdout, error = run_python(code, context['python_env'], timeout_seconds=300)
            
            if self.logger.print_to_console_default:
                if stdout:
                    print(f"[stdout]\n{stdout}")
                if error:
                    print(f"[error]\n{error}")
            
            if stdout:
                log_parts.append(f"[stdout]\n{stdout}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'stdout': stdout, 'error': error}, ensure_ascii=False)
        
        elif name == 'run_wolfram':
            code = args.get('code', '')
            if self.logger.print_to_console_default:
                print(f"[Tool Call] run_wolfram\nCode:\n{code}")
            
            log_parts.append(f"Code:\n{code}")
            
            if context['wolfram_session'] is None:
                error = "Wolfram session not available"
                output = ""
            else:
                output, error = run_wolfram(code, context['wolfram_session'])
            
            if self.logger.print_to_console_default:
                if output:
                    print(f"[output]\n{output}")
                if error:
                    print(f"[error]\n{error}")
            
            if output:
                log_parts.append(f"[output]\n{output}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'output': output, 'error': error}, ensure_ascii=False)
        
        elif name == 'math_research_subagent':
            task_description = args.get('task_description', '')
            if self.logger.print_to_console_default:
                print(f"[Tool Call] math_research_subagent\nTask:\n{task_description}")
            
            log_parts.append(f"Task:\n{task_description}")
            result, error = run_subagent(task_description, self.logger.print_to_console_default, self.logger)
            
            if self.logger.print_to_console_default:
                if result:
                    print(f"[result]\n{result}")
                if error:
                    print(f"[error]\n{error}")
            
            if result:
                log_parts.append(f"[result]\n{result}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'result': result, 'error': error}, ensure_ascii=False)
        
        elif name == 'solver_format_guard':
            candidate_response = args.get('candidate_response', '')
            if self.logger.print_to_console_default:
                print(f"[Tool Call] solver_format_guard\nCandidate response length: {len(candidate_response)}")
            
            if candidate_response:
                log_parts.append(f"Candidate response length: {len(candidate_response)}")
            
            result, error = solver_format_guard(candidate_response)
            
            if self.logger.print_to_console_default:
                if result:
                    print(f"[result]\n{result}")
                if error:
                    print(f"[error]\n{error}")
            
            if result:
                log_parts.append(f"[result]\n{result}")
            if error:
                log_parts.append(f"[error]\n{error}")
            
            tool_content = json.dumps({'result': result, 'error': error}, ensure_ascii=False)
        
        else:
            if self.logger.print_to_console_default:
                print(f"[Tool Call] {name} (not implemented)")
            log_parts.append("(not implemented)")
            tool_content = json.dumps({'error': f'tool {name} not implemented in client'}, ensure_ascii=False)

        # Always write tool call + outputs to the log file as a single entry.
        # This keeps the log readable and avoids the "divider appears early" issue
        # caused by mixing per-fragment streaming logs with later buffered logs.
        self.logger.log_print("[工具调用详情]\n" + "\n".join(log_parts).lstrip("\n"))

        return tool_content, log_parts

