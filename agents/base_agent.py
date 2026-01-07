"""
基础Agent类，为所有Agent提供通用功能和结构化的prep-exec-post工作流。

BaseAgent provides:
- Common validation methods for prep/exec results
- Unified LLM interaction with logging
- Context rendering utilities
- Error handling patterns
- Template methods for agents to override
"""

import time
import json
from typing import Any, Dict, List, Optional, Tuple
from pocketflow import Node
from config.agent_config import AlphaSolveConfig
from llms.utils import LLMClient
from utils.logger import Logger


class BaseAgent(Node):
    """
    所有Agent的基类，提供prep-exec-post工作流的通用功能。
    
    工作流说明：
    1. prep(shared): 从shared中读取数据，准备执行所需的信息 [只读shared]
    2. exec(prep_res): 执行主要逻辑（如调用LLM），不访问shared
    3. post(shared, prep_res, exec_res): 根据执行结果更新shared [只写shared]
    
    子类需要实现：
    - _prepare_data(shared): 具体的准备逻辑
    - _execute_logic(prep_res): 具体的执行逻辑
    - _update_shared(shared, prep_res, exec_res): 具体的更新逻辑
    """

    def __init__(self, llm: LLMClient, logger: Logger, module_name: str):
        """
        初始化BaseAgent。
        
        Args:
            llm: LLM客户端，用于调用语言模型
            logger: 日志记录器
            module_name: 模块名称，用于日志记录（如 "solver", "verifier"）
        """
        super(BaseAgent, self).__init__()
        self.llm = llm
        self.logger = logger
        self.module_name = module_name
        self.print_to_console = logger.print_to_console_default if hasattr(logger, 'print_to_console_default') else True

    # ========================================
    # 主工作流方法（由子类实现具体逻辑）
    # ========================================

    def prep(self, shared: Dict[str, Any]) -> Tuple[int, ...]:
        """
        准备阶段：从shared中读取数据，验证状态。
        
        子类应实现 _prepare_data(shared) 来提供具体逻辑。
        
        Args:
            shared: 共享上下文（只读）
            
        Returns:
            Tuple[int, ...]: (status_code, *data) 形式的元组
                - status_code: AlphaSolveConfig中定义的状态码
                - data: 传递给exec的数据
        """
        try:
            return self._prepare_data(shared)
        except Exception as e:
            self.logger.log_print(
                f"event=prep_exception step=prep error={str(e)}",
                module=self.module_name,
                level="ERROR",
            )
            return (AlphaSolveConfig.EXIT_ON_ERROR, None)

    def exec(self, prep_res: Tuple[Any, ...]) -> Tuple[int, ...]:
        """
        执行阶段：执行主要逻辑（如LLM调用）。
        
        子类应实现 _execute_logic(prep_res) 来提供具体逻辑。
        
        Args:
            prep_res: prep阶段返回的结果
            
        Returns:
            Tuple[int, ...]: (status_code, *data) 形式的元组
        """
        # 验证prep结果的基本结构
        if not self._validate_prep_result(prep_res):
            return (AlphaSolveConfig.EXIT_ON_ERROR, None)
        
        try:
            return self._execute_logic(prep_res)
        except Exception as e:
            self.logger.log_print(
                f"event=exec_exception step=exec error={str(e)}",
                module=self.module_name,
                level="ERROR",
            )
            return (AlphaSolveConfig.EXIT_ON_ERROR, None)

    def post(self, shared: Dict[str, Any], prep_res: Tuple[Any, ...], exec_res: Tuple[Any, ...]) -> int:
        """
        后处理阶段：根据执行结果更新shared。
        
        子类应实现 _update_shared(shared, prep_res, exec_res) 来提供具体逻辑。
        
        Args:
            shared: 共享上下文（只写）
            prep_res: prep阶段返回的结果
            exec_res: exec阶段返回的结果
            
        Returns:
            int: 状态码，决定工作流的下一步
        """
        # 验证exec结果的基本结构
        if not self._validate_exec_result(exec_res):
            self.logger.log_print(
                "event=illegal_exec_res step=post",
                module=self.module_name,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR
        
        try:
            return self._update_shared(shared, prep_res, exec_res)
        except Exception as e:
            self.logger.log_print(
                f"event=post_exception step=post error={str(e)}",
                module=self.module_name,
                level="ERROR",
            )
            return AlphaSolveConfig.EXIT_ON_ERROR

    # ========================================
    # 子类需要实现的抽象方法
    # ========================================

    def _prepare_data(self, shared: Dict[str, Any]) -> Tuple[int, ...]:
        """
        子类实现：准备执行所需的数据。
        
        从shared中读取数据，构建消息/提示词，验证状态等。
        注意：只能读取shared，不能修改！
        
        Args:
            shared: 共享上下文
            
        Returns:
            Tuple[int, ...]: (status_code, *data)
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement _prepare_data()")

    def _execute_logic(self, prep_res: Tuple[Any, ...]) -> Tuple[int, ...]:
        """
        子类实现：执行主要逻辑。
        
        通常包括：
        - 解析prep_res
        - 调用LLM（使用 _call_llm 辅助方法）
        - 处理结果
        
        Args:
            prep_res: prep阶段的返回值
            
        Returns:
            Tuple[int, ...]: (status_code, *data)
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement _execute_logic()")

    def _update_shared(self, shared: Dict[str, Any], prep_res: Tuple[Any, ...], exec_res: Tuple[Any, ...]) -> int:
        """
        子类实现：根据执行结果更新shared。
        
        注意：只能写入shared，不应该读取（所有需要的数据应该在prep_res和exec_res中）
        
        Args:
            shared: 共享上下文
            prep_res: prep阶段的返回值
            exec_res: exec阶段的返回值
            
        Returns:
            int: 状态码
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement _update_shared()")

    # ========================================
    # 通用工具方法
    # ========================================

    def _validate_prep_result(self, prep_res: Any, min_length: int = 1) -> bool:
        """
        验证prep结果的基本结构。
        
        Args:
            prep_res: prep阶段返回的结果
            min_length: 期望的最小长度
            
        Returns:
            bool: 验证是否通过
        """
        if not prep_res:
            self.logger.log_print(
                "event=empty_prep_res step=exec",
                module=self.module_name,
                level="ERROR",
            )
            return False
        
        if not isinstance(prep_res, (tuple, list)):
            self.logger.log_print(
                f"event=invalid_prep_res_type step=exec type={type(prep_res)}",
                module=self.module_name,
                level="ERROR",
            )
            return False
        
        if len(prep_res) < min_length:
            self.logger.log_print(
                f"event=prep_res_too_short step=exec length={len(prep_res)} min={min_length}",
                module=self.module_name,
                level="ERROR",
            )
            return False
        
        return True

    def _validate_exec_result(self, exec_res: Any, min_length: int = 1) -> bool:
        """
        验证exec结果的基本结构。
        
        Args:
            exec_res: exec阶段返回的结果
            min_length: 期望的最小长度
            
        Returns:
            bool: 验证是否通过
        """
        if not exec_res:
            return False
        
        if not isinstance(exec_res, (tuple, list)):
            return False
        
        if len(exec_res) < min_length:
            return False
        
        return True

    def _check_quota_exhausted(
        self, 
        shared: Dict[str, Any], 
        quota_key: str,
        exhausted_code: int = AlphaSolveConfig.EXIT_ON_EXAUSTED,
        additional_data: Any = None
    ) -> Optional[Tuple[int, ...]]:
        """
        检查配额是否耗尽。
        
        这是一个常见的prep检查模式。如果配额耗尽，返回相应的状态码；
        否则返回None，表示可以继续执行。
        
        Args:
            shared: 共享上下文
            quota_key: 配额在shared中的键名（如 "solver_round_remaining"）
            exhausted_code: 配额耗尽时返回的状态码
            additional_data: 配额耗尽时返回的额外数据
            
        Returns:
            Optional[Tuple[int, ...]]: 如果配额耗尽，返回(exhausted_code, additional_data)；
                                       否则返回None
        """
        remaining = shared.get(quota_key, 1)
        if remaining == 0:
            self.logger.log_print(
                f"event=quota_exhausted step=prep quota_key={quota_key} remaining=0",
                module=self.module_name,
                level="WARNING",
            )
            if additional_data is not None:
                return (exhausted_code, additional_data)
            return (exhausted_code, None)
        return None

    def _call_llm(
        self, 
        messages: List[Dict[str, str]], 
        log_messages: bool = True
    ) -> Tuple[str, str, List[Dict[str, str]]]:
        """
        调用LLM并记录日志。
        
        这是对 self.llm.get_result() 的封装，添加了统一的日志记录。
        
        Args:
            messages: 发送给LLM的消息列表
            log_messages: 是否记录发送的消息详情
            
        Returns:
            Tuple[str, str, List]: (answer, cot, updated_messages)
                - answer: LLM的回答
                - cot: 推理过程（如果有）
                - updated_messages: 更新后的消息历史
        """
        if log_messages:
            self.logger.log_print(
                "event=llm_messages step=exec\n" + json.dumps(messages, ensure_ascii=False, indent=2),
                module=self.module_name,
            )
        
        start_time = time.time()
        answer, cot, updated_messages = self.llm.get_result(messages)
        elapsed = time.time() - start_time
        
        self.logger.log_print(
            f"event=llm_done step=exec elapsed_s={elapsed:.1f} answer_len={len(answer)} cot_len={len(cot)}",
            module=self.module_name,
        )
        
        return answer, cot, updated_messages

    def _render_context(self, ctx_ids: List[int], lemmas: List[Dict[str, Any]]) -> Optional[str]:
        """
        将已验证的引理渲染为上下文文本。
        
        这是所有Agent都会用到的通用功能：将依赖的引理格式化为文本，
        附加到提示词中。
        
        Args:
            ctx_ids: 上下文引理的ID列表
            lemmas: 所有引理的列表
            
        Returns:
            Optional[str]: 格式化的上下文文本，如果没有上下文则返回None
        """
        if not ctx_ids:
            return None
        
        lines = []
        lines.append("## Context and History Explorations")
        lines.append("")
        lines.append(
            "Here is a list of context that we have collected for this problem or our history findings during exploration. "
            "They serve as the background of the conjecture and proof and can be accepted without controversy as correct."
        )
        lines.append("")
        
        for i, lemma_id in enumerate(ctx_ids):
            if 0 <= lemma_id < len(lemmas):
                lines.append(f" ** Conjecture-{i} **")
                lines.append(f" {lemmas[lemma_id].get('statement', '')}")
        
        return "\n".join(lines)

    def _handle_status_code_in_exec(self, prep_res: Tuple[Any, ...], expected_normal: int = AlphaSolveConfig.NORMAL) -> Optional[Tuple[int, ...]]:
        """
        在exec中处理prep返回的状态码。
        
        这是一个常见的exec模式：检查prep_res的第一个元素（状态码），
        如果不是NORMAL，则提前返回相应的错误。
        
        Args:
            prep_res: prep阶段的返回值
            expected_normal: 期望的正常状态码（默认为NORMAL）
            
        Returns:
            Optional[Tuple[int, ...]]: 如果状态码异常，返回相应的错误元组；
                                       否则返回None，表示可以继续执行
        """
        if not prep_res or len(prep_res) < 1:
            return (AlphaSolveConfig.EXIT_ON_ERROR, None)
        
        code = prep_res[0]
        
        # 处理常见的异常状态码
        if code == AlphaSolveConfig.EXIT_ON_ERROR:
            return (AlphaSolveConfig.EXIT_ON_ERROR, None)
        
        if code == AlphaSolveConfig.SOLVER_EXAUSTED:
            return (AlphaSolveConfig.EXIT_ON_EXAUSTED, None)
        
        if code == AlphaSolveConfig.VERIFIER_EXAUSTED:
            return (AlphaSolveConfig.VERIFIER_EXAUSTED, True, None)
        
        # 如果状态码不是期望的正常值
        if code != expected_normal:
            self.logger.log_print(
                f"event=unexpected_status_code step=exec code={code} expected={expected_normal}",
                module=self.module_name,
                level="WARNING",
            )
            return (AlphaSolveConfig.EXIT_ON_ERROR, None)
        
        return None

    def _log_event(self, event: str, step: str = "unknown", level: str = "INFO", **kwargs):
        """
        记录事件日志的便捷方法。
        
        Args:
            event: 事件名称
            step: 当前步骤（prep/exec/post）
            level: 日志级别
            **kwargs: 其他要记录的键值对
        """
        parts = [f"event={event}", f"step={step}"]
        for k, v in kwargs.items():
            parts.append(f"{k}={v}")
        
        self.logger.log_print(
            " ".join(parts),
            module=self.module_name,
            print_to_console=self.print_to_console,
            level=level,
        )

    def _get_current_lemma(self, shared: Dict[str, Any]) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        """
        从shared中获取当前引理。
        
        这是Verifier、Refiner、DiffRefiner的常见模式。
        
        Args:
            shared: 共享上下文
            
        Returns:
            Tuple[Optional[int], Optional[Dict]]: (lemma_id, lemma)
                如果没有当前引理，返回(None, None)
        """
        lemma_id = shared.get("current_lemma_id")
        if lemma_id is None:
            self._log_event("no_current_lemma", step="prep", level="ERROR")
            return None, None
        
        lemmas = shared.get("lemmas", [])
        if lemma_id < 0 or lemma_id >= len(lemmas):
            self._log_event("invalid_lemma_id", step="prep", level="ERROR", lemma_id=lemma_id)
            return None, None
        
        lemma = lemmas[lemma_id]
        return lemma_id, lemma
