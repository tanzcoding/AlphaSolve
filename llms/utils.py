from typing import Dict, List, Tuple, Optional, Any
import os
import json
import re
from openai import OpenAI
from wolframclient.evaluation import WolframLanguageSession
from .exceptions import LLMServiceException
from .tools import *
from agents.shared_context import Lemma, SharedContext
from copy import deepcopy

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
    
    def get_result(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        shared: Optional[SharedContext] = None,
    ) -> Tuple[str, str, List[Dict]]:
        """
        统一的获取 LLM 回复方法，始终使用流式输出

        NOTE: 为避免流式输出在达到最大输出长度/中断时卡住工作流，
        这里增加了“整次重新生成”的重试机制（不续写上一次内容）。

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            tools: 工具列表（可选）。如果为None，使用self.tools；如果明确传入[]，则不使用工具

        Returns:
            Tuple[str, str, List[Dict]]: (answer_content, reasoning_content, updated_messages)
        """
        from config.agent_config import AlphaSolveConfig

        b = time.time()
        # 确定使用的工具列表
        if tools is None:
            tools = self.tools if self.tools else []

        logger = self.logger

        # IMPORTANT: retry must restart from the same input messages (no continuation).
        base_messages = deepcopy(messages)

        # 初始化工具执行环境
        tool_context = self._init_tool_context(tools) if tools else {}

        # Expose shared to tool executor (the model does NOT see `shared` directly).
        # Tools like read_lemma(lemma_id) / read_conjecture() can fetch lemma text from this context.
        if tool_context is not None:
            tool_context['shared'] = shared

        if shared is not None and len(shared['lemmas']) <= 1:
            tools = [t for t in tools if t.get('function', {}).get('name') != 'read_lemma']

        max_api_retry = getattr(AlphaSolveConfig, 'MAX_API_RETRY', 8)

        # 多轮对话直至没有工具调用
        max_iterations = 100
        attempt = 0
        last_exc: Optional[Exception] = None

        try:
            while attempt < max_api_retry:
                attempt += 1

                reasoning_content = ""
                answer_content = ""
                # Restart the whole conversation for this call.
                messages = deepcopy(base_messages)

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
                            last_exc = None
                            break

                        # 处理工具调用
                        logger.log_print("\n" + "-" * 10 + "思维链中工具调用" + "-" * 10)

                        for tc in tool_calls:
                            name = tc['function']['name']
                            raw_args = tc['function'].get('arguments') or '{}'
                            args, parse_error = self._parse_tool_arguments(raw_args, shared)
                            if args is None:
                                warning_msg = (
                                    f"event=tool_args_parse_error name={name} error={parse_error}"
                                )
                                logger.log_print(warning_msg, module=self.module, level="ERROR")
                                error_payload = json.dumps({'error': parse_error}, ensure_ascii=False)
                                reasoning_content += (warning_msg + "\n")
                                messages.append({
                                    'role': 'tool',
                                    'tool_call_id': tc.get('id'),
                                    'name': name,
                                    'content': error_payload,
                                })
                                continue

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

                    # If we exited the loop without exception and last_exc is None, we are done.
                    if last_exc is None:
                        logger.log_print(
                            f"\nevent=llm_done step=exec elapsed_s={time.time() - b:.1f} answer_len={len(answer_content)} cot_len={len(reasoning_content)} retries_used={attempt-1}",
                            module=self.module,
                        )
                        return answer_content, reasoning_content, messages

                except LLMServiceException as exc:
                    last_exc = exc
                    logger.log_print(
                        f"\nevent=llm_retry attempt={attempt}/{max_api_retry} status={getattr(exc, 'status', None)} error={exc}",
                        module=self.module,
                        level="ERROR",
                    )
                    if attempt >= max_api_retry:
                        raise
                    continue
                except Exception as exc:
                    # Unknown exception: still retry because stream can fail in various ways.
                    last_exc = exc
                    logger.log_print(
                        f"\nevent=llm_retry attempt={attempt}/{max_api_retry} status=unknown_exception error={exc}",
                        module=self.module,
                        level="ERROR",
                    )
                    if attempt >= max_api_retry:
                        raise
                    continue

            # Should be unreachable
            if last_exc is not None:
                raise last_exc
            return "", "", deepcopy(base_messages)

        finally:
            # 清理工具上下文
            if tool_context:
                self._cleanup_tool_context(tool_context)
        
    def _parse_tool_arguments(
        self,
        raw_args: str,
        shared: Optional[SharedContext] = None,
    ) -> Tuple[Optional[Dict], Optional[str]]:
        """Best-effort parsing of tool arguments emitted by the LLM."""
        # Remove common LLM output artifacts that may appear after JSON
        # Strategy: Find the first complete JSON object/array, discard everything after
        
        cleaned = raw_args.strip()
        
        # First, remove <|xxx|> markers and everything after them
        cleaned = re.sub(r'<\|[^>]+\|>.*$', '', cleaned)
        cleaned = cleaned.strip()
        
        # Try to find where the valid JSON ends by matching braces/brackets
        # For a JSON object starting with {, find the matching }
        # For a JSON array starting with [, find the matching ]
        if cleaned and cleaned[0] in ('{', '['):
            opening = cleaned[0]
            closing = '}' if opening == '{' else ']'
            depth = 0
            in_string = False
            escape_next = False
            
            for i, char in enumerate(cleaned):
                if escape_next:
                    escape_next = False
                    continue
                    
                if char == '\\':
                    escape_next = True
                    continue
                
                if char == '"' and not in_string:
                    in_string = True
                elif char == '"' and in_string:
                    in_string = False
                elif not in_string:
                    if char == opening:
                        depth += 1
                    elif char == closing:
                        depth -= 1
                        if depth == 0:
                            # Found the end of the JSON structure
                            cleaned = cleaned[:i+1]
                            break
        
        candidates = [cleaned]
        
        # Strategy: Aggressive backslash escaping for LaTeX
        # Replace all backslashes with doubled backslashes, then restore valid JSON escapes
        fixed = cleaned.replace('\\', '\\\\')  # First, double all backslashes
        # Now restore the over-escaped valid JSON sequences
        fixed = fixed.replace('\\\\\\\\', '\\\\')  # \\\\ -> \\ (was already escaped)
        fixed = fixed.replace('\\\\"', '\\"')      # \\" -> \" (quote)
        fixed = fixed.replace('\\\\/', '\\/')      # \\/ -> \/ (slash, optional)
        fixed = fixed.replace('\\\\b', '\\b')      # \\b -> \b (backspace)
        fixed = fixed.replace('\\\\f', '\\f')      # \\f -> \f (formfeed)
        fixed = fixed.replace('\\\\n', '\\n')      # \\n -> \n (newline)
        fixed = fixed.replace('\\\\r', '\\r')      # \\r -> \r (carriage return)
        fixed = fixed.replace('\\\\t', '\\t')      # \\t -> \t (tab)
        fixed = fixed.replace('\\\\u', '\\u')      # \\u -> \u (unicode)
        if fixed != cleaned:
            candidates.append(fixed)

        # Escape literal control characters that break JSON decoding
        escaped_controls = (cleaned
                            .replace('\r', r'\r')
                            .replace('\n', r'\n')
                            .replace('\t', r'\t'))
        if escaped_controls != cleaned:
            candidates.append(escaped_controls)
        
        # Combine both: escape controls then fix backslashes
        combined = escaped_controls.replace('\\', '\\\\')
        combined = combined.replace('\\\\\\\\', '\\\\')
        combined = combined.replace('\\\\"', '\\"')
        combined = combined.replace('\\\\/', '\\/')
        combined = combined.replace('\\\\b', '\\b')
        combined = combined.replace('\\\\f', '\\f')
        combined = combined.replace('\\\\n', '\\n')
        combined = combined.replace('\\\\r', '\\r')
        combined = combined.replace('\\\\t', '\\t')
        combined = combined.replace('\\\\u', '\\u')
        if combined not in candidates:
            candidates.append(combined)

        last_error: Optional[Exception] = None
        parsed_result = None
        
        # Try to parse JSON with various escape strategies
        for candidate in candidates:
            for strict in (True, False):
                try:
                    parsed_result = json.loads(candidate, strict=strict)
                    break
                except json.JSONDecodeError as exc:
                    last_error = exc
                    continue
            if parsed_result is not None:
                break
        
        # If parsing failed, return error
        if parsed_result is None:
            error_msg = "failed to decode tool arguments"
            if last_error is not None:
                error_msg = f"{error_msg}: {last_error}"
            return None, error_msg
        
        # Special handling for editing tools: add lemma context
        if isinstance(parsed_result, dict):
            # Normalize common over-escaping in markers.
            # The model may output markers with doubled backslashes (e.g. "\\\\eta")
            # even when the proof text contains single backslashes ("\\eta").
            # We only normalize markers (not the replacement body) to preserve intent.
            for _k in ("begin_marker", "end_marker"):
                v = parsed_result.get(_k)
                if isinstance(v, str) and "\\\\" in v:
                    # Collapse repeated double-backslashes down to single-backslashes.
                    # Repeat until stable so 4->2->1 style over-escaping is handled.
                    while "\\\\" in v:
                        v = v.replace("\\\\", "\\")
                    parsed_result[_k] = v

            has_new_statement = 'new_statement' in parsed_result
            has_begin_marker = 'begin_marker' in parsed_result
            has_end_marker = 'end_marker' in parsed_result
            has_proof_replacement = 'proof_replacement' in parsed_result

            if (
                has_new_statement
                or has_begin_marker
                or has_end_marker
                or has_proof_replacement
            ):
                # This is an editing operation, add lemma context if available
                lemma = None
                if shared is not None:
                    lemma_id = shared['current_lemma_id']
                    lemmas = shared['lemmas']
                    if lemma_id is not None and 0 <= lemma_id < len(lemmas):
                        lemma = lemmas[lemma_id]
                if lemma is not None:
                    parsed_result['lemma'] = lemma
        
        return parsed_result, None

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
            logger.log_print("\n" + "-" * 10 + "继续思考" + "-" * 10)
        else:
            logger.log_print("\n" + "=" * 20 + "思维链内容" + "=" * 20)
            if 'gpt' in self.model or 'gemini' in self.model or 'claude' in self.model or 'o4' in self.model or 'grok' in self.model or 'o3' in self.model or 'o1' in self.model:
                logger.log_print("此模型不返回思维链内容，以下仅显示模型可能给出的 reasoning_content 与最终回答\n")

        # 创建流式请求
        stream = self.client.chat.completions.create(
            messages=messages,
            tools=tools,
            tool_choice='auto' if tools else None,
            stream=True,
            ## logprobs=True,     
            ## top_logprobs=5,
            **model_params,
            **({"extra_body": extra_body} if extra_body else {})
        )

        # 流式读取并拼接
        finish_reason = None
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(choice, 'finish_reason', None):
                finish_reason = choice.finish_reason

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
                    logger.log_print("\n" + "=" * 20 + "最终回答" + "=" * 20)
                    is_answering = True
                logger.log_print(ct_part, end="")
                content_parts.append(ct_part)

            # 处理 tool_calls
            tc_delta = getattr(delta, 'tool_calls', None)
            if tc_delta:
                self._process_tool_calls(tc_delta, tool_calls_acc)

        # 检查结束原因
        if not finish_reason:
            raise LLMServiceException("LLM response missing finish_reason", status="missing_finish_reason")
        if finish_reason not in ("stop", "tool_calls"):
            raise LLMServiceException(
                f"LLM response interrupted, finish_reason={finish_reason}",
                status=finish_reason,
            )

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
        for fallback_i, tc in enumerate(tc_delta):
            # IMPORTANT:
            # In streamed tool calls, each delta may carry an explicit `index`.
            # Do NOT rely on enumeration order; otherwise multiple tool calls can
            # be merged incorrectly when the stream yields sparse indices.
            if isinstance(tc, dict):
                idx = tc.get('index', fallback_i)
                tc_id = tc.get('id')
                fn = tc.get('function')
            else:
                idx = getattr(tc, 'index', fallback_i)
                tc_id = getattr(tc, 'id', None)
                fn = getattr(tc, 'function', None)

            if not isinstance(idx, int) or idx < 0:
                idx = fallback_i

            while len(tool_calls_acc) <= idx:
                tool_calls_acc.append({'id': None, 'type': 'function', 'function': {'name': '', 'arguments': ''}})

            if tc_id:
                tool_calls_acc[idx]['id'] = tc_id

            if fn:
                if isinstance(fn, dict):
                    fn_name = fn.get('name')
                    fn_args = fn.get('arguments')
                else:
                    fn_name = getattr(fn, 'name', None)
                    fn_args = getattr(fn, 'arguments', None)

                if fn_name:
                    tool_calls_acc[idx]['function']['name'] = fn_name
                if fn_args:
                    tool_calls_acc[idx]['function']['arguments'] += fn_args

    def _init_tool_context(self, tools: List[Dict]) -> Dict:
        """初始化工具执行上下文"""
        context = {
            'python_env': {},
            'wolfram_session': None,
        }

        # 检查是否需要Wolfram session
        needs_wolfram = any(tool.get('function', {}).get('name') == 'run_wolfram' for tool in tools)
        if needs_wolfram:
            context['wolfram_session'] = self._start_wolfram_session()

        return context

    def _start_wolfram_session(self) -> Optional[WolframLanguageSession]:
        if self.logger.print_to_console_default:
            print("[正在启动 Wolfram Language Session...]\n", end="")

        # Prefer the default startup mechanism first. If it fails, fall back to
        # an explicit kernel path specified via environment variable WOLFRAM_KERNEL.
        try:
            session = WolframLanguageSession()
            if self.logger.print_to_console_default:
                print("[Wolfram Language Session 已启动]\n", end="")
            return session
        except Exception as exc_default:
            kernel_path = os.environ.get("WOLFRAM_KERNEL")
            if kernel_path:
                try:
                    session = WolframLanguageSession(kernel_path)
                    if self.logger.print_to_console_default:
                        print("[Wolfram Language Session 已启动 (via WOLFRAM_KERNEL)]\n", end="")
                    return session
                except Exception as exc_env:
                    if self.logger.print_to_console_default:
                        print(
                            f"[警告] Wolfram session 启动失败: default={exc_default}; WOLFRAM_KERNEL={kernel_path!r} error={exc_env}\n",
                            end="",
                        )
                    return None

            if self.logger.print_to_console_default:
                print(
                    f"[警告] Wolfram session 启动失败: default={exc_default}; env WOLFRAM_KERNEL not set\n",
                    end="",
                )
            return None
    
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
            
            tool_content = ""
            if stdout:
                tool_content = tool_content + f"[stdout]\n{stdout}"
                log_parts.append(f"[stdout]\n{stdout}")
            if error:
                tool_content = tool_content + f"[error]\n{error}"
                log_parts.append(f"[error]\n{error}")
        
        elif name == 'run_wolfram':
            code = args.get('code', '')
            if self.logger.print_to_console_default:
                print(f"[Tool Call] run_wolfram\nCode:\n{code}")
            
            log_parts.append(f"Code:\n{code}")

            session = context.get('wolfram_session')
            if session is None:
                session = self._start_wolfram_session()
                context['wolfram_session'] = session

            if session is None:
                error = "Wolfram session not available"
                output = ""
            else:
                output, error = run_wolfram(code, session)

            if isinstance(error, str) and error.startswith('timeout'):
                context['wolfram_session'] = self._start_wolfram_session()

            if self.logger.print_to_console_default:
                if output:
                    print(f"[output]\n{output}")
                if error:
                    print(f"[error]\n{error}")
            
            tool_content = ""
            if output:
                tool_content = tool_content + f"[output]\n{output}"
                log_parts.append(f"[output]\n{output}")
            if error:
                tool_content = tool_content + f"[error]\n{error}"
                log_parts.append(f"[error]\n{error}")
        
        elif name == 'math_research_subagent':
            task_description = args.get('task_description', '')
            shared = context.get('shared')
            if self.logger.print_to_console_default:
                print(f"[Tool Call] math_research_subagent\nTask:\n{task_description}")
            
            log_parts.append(f"Task:\n{task_description}")
            result, error = run_subagent(task_description, self.logger, shared)
            
            if self.logger.print_to_console_default:
                if result:
                    print(f"[result]\n{result}")
                if error:
                    print(f"[error]\n{error}")
            
            tool_content = ''
            if result:
                log_parts.append(f"[result]\n{result}")
                tool_content = tool_content + f"[result]\n{result}"
            if error:
                log_parts.append(f"[error]\n{error}")
                tool_content = tool_content + f"[error]\n{error}"
        
        elif name == 'solver_response_format_reminder':
            if self.logger.print_to_console_default:
                print("[Tool Call] solver_response_format_reminder")

            result, error = solver_response_format_reminder()

            if self.logger.print_to_console_default:
                if result:
                    print(f"[result]\n{result}")
                if error:
                    print(f"[error]\n{error}")

            if result:
                log_parts.append(f"[result]\n{result}")
            if error:
                log_parts.append(f"[error]\n{error}")

            tool_content = result if result else ""
        
        elif name == 'refiner_response_format_reminder':
            if self.logger.print_to_console_default:
                print("[Tool Call] refiner_response_format_reminder")

            result, error = refiner_response_format_reminder()
            if self.logger.print_to_console_default:
                if result:
                    print(f"[result]\n{result}")
                if error:
                    print(f"[error]\n{error}")

            if result:
                log_parts.append(f"[result]\n{result}")
            if error:
                log_parts.append(f"[error]\n{error}")

            tool_content = result if result else ""

        elif name == 'refine_conjecture_with_diff':
            tool_content = json.dumps({'error': 'refine_conjecture_with_diff is deprecated'}, ensure_ascii=False)
        elif name == 'modify_statement':
            new_statement = args.get('new_statement', '')
            lemma: Optional[Lemma] = args.get('lemma') or context.get('current_lemma')

            if lemma is None:
                error = "No lemma provided for modify_statement"
                log_parts.append(f"[error]\n{error}")
                tool_content = json.dumps({'error': error}, ensure_ascii=False)
            else:
                if self.logger.print_to_console_default:
                    print("[Tool Call] modify_statement\nNew statement preview:\n"
                          f"{new_statement[:200]}...")
                log_parts.append(f"New statement length: {len(new_statement)}")
                result = apply_new_statement_to_lemma(lemma, new_statement)
                log_parts.append(f"[result]\n{result}")
                tool_content = result

        elif name == 'modify_proof':
            begin_marker = args.get('begin_marker', '')
            end_marker = args.get('end_marker', '')
            proof_replacement = args.get('proof_replacement', '')
            lemma: Optional[Lemma] = args.get('lemma') or context.get('current_lemma')

            if lemma is None:
                error = "No lemma provided for modify_proof"
                log_parts.append(f"[error]\n{error}")
                tool_content = json.dumps({'error': error}, ensure_ascii=False)
            else:
                if self.logger.print_to_console_default:
                    print("[Tool Call] modify_proof\n"
                          f"begin_marker={begin_marker[:100]}\nend_marker={end_marker[:100]}\n"
                          f"first 200 replacement={proof_replacement[:200]}...")
                log_parts.append(
                    "Proof anchors lengths: begin={} end={} replacement={}".format(
                        len(begin_marker),
                        len(end_marker),
                        len(proof_replacement),
                    )
                )
                result = apply_proof_anchor_edit(lemma, begin_marker, end_marker, proof_replacement)
                log_parts.append(f"[result]\n{result}")
                tool_content = result

        elif name == 'read_lemma':
            lemma_id = args.get('lemma_id', None)
            shared = context.get('shared')

            if shared is None:
                tool_content = (
                    "[read_lemma error] shared context is not provided. "
                    "Enable it by calling LLMClient.get_result(..., shared=shared)."
                )
                log_parts.append(f"[error]\n{tool_content}")
            else:
                try:
                    if not isinstance(lemma_id, int):
                        raise TypeError("lemma_id must be an integer")

                    lemmas = shared['lemmas']
                    if lemmas is None:
                        raise ValueError("Currently no existing lemmas!")
                    if len(lemmas) == 0:
                        raise ValueError("Currently no existing lemmas!")
                    if lemma_id < 0 or lemma_id >= len(lemmas):
                        raise IndexError(f"lemma_id out of range: {lemma_id}")

                    lemma = lemmas[lemma_id]
                    log_parts.append(f"lemma_id={lemma_id}")

                    if lemma['status'] != 'verified':
                        tool_content = "[read_lemma warning] This lemma has been rejected previously, Reading this lemma is prohibited."
                        verified_lemma_ids = [i for i, l in enumerate(lemmas) if l.get('status') == 'verified']
                        if verified_lemma_ids:
                            tool_content += f" Verified lemma IDs: {verified_lemma_ids}"
                    else:
                        statement = lemma.get('statement', '')
                        proof = lemma.get('proof', '')
                        tool_content = (
                            f"## Lemma-{lemma_id}\n\n"
                            f"{statement}\n\n"
                            f"## Proof\n\n"
                            f"{proof}"
                        )
                    log_parts.append(tool_content)
                except Exception as exc:
                    tool_content = f"[read_lemma error] {exc}"
                    log_parts.append(f"[error]\n{tool_content}")

        elif name in ('read_conjecture', 'read_current_conjecture_again'):
            shared = context.get('shared')

            if shared is None:
                tool_content = (
                    f"[{name} error] shared context is not provided. "
                    "Enable it by calling LLMClient.get_result(..., shared=shared)."
                )
                log_parts.append(f"[error]\n{tool_content}")
            else:
                try:
                    lemma_id = shared['current_lemma_id']
                    if lemma_id is None:
                        raise ValueError("current_lemma_id is not set")
                    if not isinstance(lemma_id, int):
                        raise TypeError("current_lemma_id must be an integer")

                    lemmas = shared['lemmas']
                    if lemmas is None:
                        raise ValueError("Currently no lemmas in shared context")
                    if lemma_id < 0 or lemma_id >= len(lemmas):
                        raise IndexError(f"current_lemma_id out of range: {lemma_id}")

                    lemma = lemmas[lemma_id]
                    statement = lemma.get('statement', '')
                    proof = lemma.get('proof', '')
                    content = (
                        r"\begin{conjecture}\n"
                        f"{statement}\n"
                        r"\end{conjecture}\n"
                        r"\begin{proof}\n"
                        f"{proof}\n"
                        r"\end{proof}"
                    )
                    # IMPORTANT: return plain text (NOT JSON) to preserve backslashes (LaTeX).
                    tool_content = content
                    log_parts.append(content)
                    log_parts.append(f"[result]\n(len={len(tool_content)})")
                except Exception as exc:
                    tool_content = f"[{name} error] {exc}"
                    log_parts.append(f"[error]\n{tool_content}")

        elif name == 'read_review_again':
            shared = context.get('shared')

            if shared is None:
                tool_content = (
                    f"[{name} error] shared context is not provided. "
                    "Enable it by calling LLMClient.get_result(..., shared=shared)."
                )
                log_parts.append(f"[error]\n{tool_content}")
            else:
                try:
                    lemma_id = shared['current_lemma_id']
                    if lemma_id is None:
                        raise ValueError("current_lemma_id is not set")
                    if not isinstance(lemma_id, int):
                        raise TypeError("current_lemma_id must be an integer")

                    lemmas = shared['lemmas']
                    if lemmas is None:
                        raise ValueError("Currently no lemmas in shared context")
                    if lemma_id < 0 or lemma_id >= len(lemmas):
                        raise IndexError(f"current_lemma_id out of range: {lemma_id}")

                    lemma = lemmas[lemma_id]
                    statement = lemma.get('review', '')
                    content = (
                        r"\begin{review}\n"
                        f"{statement}\n"
                        r"\end{review}"
                    )
                    # IMPORTANT: return plain text (NOT JSON) to preserve backslashes (LaTeX).
                    tool_content = content
                    log_parts.append(content)
                    log_parts.append(f"[result]\n(len={len(tool_content)})")
                except Exception as exc:
                    tool_content = f"[{name} error] {exc}"
                    log_parts.append(f"[error]\n{tool_content}")
        
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

