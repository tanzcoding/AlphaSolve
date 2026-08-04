"""Agent 上下文压缩策略。

为不同角色（generator、research_reviewer 等）提供 ``AgentContextPolicy`` 实现，
让长跑 agent 在 token 接近窗口限制前压缩历史，而不是硬撞 ``max_turns`` 墙。

设计参考 Claude Code 的 compact 机制（user 消息逐字保留 + 模型轨迹摘要）与
Codex 的 Memento 策略。数学安全原则：

- 已验证命题的精确 Statement 永远通过 Read 工具可重新获取，不在摘要中改写
- system prompt 逐字保留
- 任务定义（前几条 user 消息）逐字保留
- 中间探索轨迹被替换为简短摘要
- 最近 N 轮完整交互保留，确保连续性
"""
from __future__ import annotations

from typing import Callable

from alphasolve.agent import AgentContextPolicy, AgentContextPolicyInput
from alphasolve.llm.types import Message


def _is_system_or_task_message(msg: Message, index: int) -> bool:
    """前几条消息（system + task 定义）应逐字保留。"""
    if msg.role == "system":
        return True
    return index < 3  # 前 2 条非 system 消息（任务 + hint）


def make_reviewer_context_policy(
    *,
    keep_recent_pairs: int = 12,
    start_turn: int = 20,
) -> AgentContextPolicy:
    """Research reviewer 的上下文压缩策略。

    - 前 ``start_turn`` 轮不压缩，让 reviewer 充分自适应探索
    - 之后保留：system prompt + 任务定义 + 最近 ``keep_recent_pairs`` 轮完整交互
    - 中间插入压缩摘要，提示 reviewer 已读过的文件可通过 Read 重新获取
    """

    def policy(input: AgentContextPolicyInput) -> list[Message]:
        messages = input.messages
        turn = input.turn

        if turn <= start_turn:
            return messages

        # 无损层：system prompt + 前 3 条消息（任务定义）
        head: list[Message] = []
        for i, msg in enumerate(messages):
            if _is_system_or_task_message(msg, i) or i < 4:
                head.append(msg)
            else:
                break

        # 最近 N 轮（每轮 = user + assistant，可能含 tool_calls/tool_results）
        keep_messages = keep_recent_pairs * 4  # 每轮约 2-4 条消息
        tail = messages[-keep_messages:] if len(messages) > keep_messages else messages[-40:]

        if len(head) + len(tail) >= len(messages):
            return messages  # 没东西可压

        compressed_count = len(messages) - len(head) - len(tail)
        summary = Message(
            role="user",
            content=(
                f"[CONTEXT COMPACTED: {compressed_count} intermediate messages omitted. "
                "Earlier exploration of verified_propositions/ and knowledge/ has been compressed. "
                "Key files you already read remain available via the Read tool — re-read only if you "
                "need exact line-level detail. Continue from the current state and focus on forming "
                "your strategic recommendations.]"
            ),
        )

        return head + [summary] + tail

    return policy


def make_generator_context_policy(
    *,
    keep_recent_pairs: int = 15,
    start_turn: int = 15,
) -> AgentContextPolicy:
    """Generator（worker 主推理）的上下文压缩策略。

    比 reviewer 更宽松——generator 需要更多近期上下文来维持证明的连贯性。
    """

    def policy(input: AgentContextPolicyInput) -> list[Message]:
        messages = input.messages
        turn = input.turn

        if turn <= start_turn:
            return messages

        # 无损层：system prompt + 任务/hint
        head: list[Message] = []
        for i, msg in enumerate(messages):
            if _is_system_or_task_message(msg, i) or i < 4:
                head.append(msg)
            else:
                break

        keep_messages = keep_recent_pairs * 4
        tail = messages[-keep_messages:] if len(messages) > keep_messages else messages[-50:]

        if len(head) + len(tail) >= len(messages):
            return messages

        compressed_count = len(messages) - len(head) - len(tail)
        summary = Message(
            role="user",
            content=(
                f"[CONTEXT COMPACTED: {compressed_count} intermediate messages omitted. "
                "Earlier exploration, file reads, and subagent calls have been compressed. "
                "All previously read verified proposition Statements remain available via Read. "
                "Continue your proof/construction from the current state.]"
            ),
        )

        return head + [summary] + tail

    return policy


def context_policy_for_subagent(
    agent_type: str,
) -> AgentContextPolicy | None:
    """按 subagent 类型返回合适的 context policy，或 None（不压缩）。

    由 ``SubagentService`` 调用：创建子 agent 时，根据 agent_type 决定是否注入
    压缩策略。当前只为 ``research_reviewer`` 注入；其它子 agent 保持原状。
    """
    if agent_type == "research_reviewer":
        return make_reviewer_context_policy()
    return None
