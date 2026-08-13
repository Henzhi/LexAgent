"""
Agent 共享状态定义。

所有节点通过 TypedDict 约定的 state 进行通信。
LangGraph 的 add_messages reducer 自动合并多轮消息。
"""
from __future__ import annotations

from typing import TypedDict, Annotated

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """多 Agent 工作流共享状态

    各节点读取/写入对应的 key，LangGraph 自动处理状态传递。

    M1 新增（D6）：
      tool_calls / tool_results / agent_turns / tool_log（ReAct 循环）；
      sub_agent（M3 预留，本期恒为 None）。
    """
    query: str                      # 原始用户查询
    messages: Annotated[list, add_messages]  # 当前会话对话历史（含工具回灌消息）
    retrieved_docs: list[dict]      # 检索结果 [{"content", "law_name", "article_range", "citation"}]
    answer: str                     # 生成的最终回答
    validation_passed: bool         # 校验是否通过
    validation_feedback: str        # 校验失败时的反馈信息（用于重试）
    retry_count: int                # 已重试次数
    is_legal_query: bool            # 意图识别：是否法律问题
    query_type: str                 # 查询类型: law_lookup | case_query | casual
    memory_context: str             # 历史对话记忆上下文（注入 Prompt）
    user_id: str                    # 用户 ID（用于记忆检索）
    # ---- M1 ReAct 工具调用（D6）----
    tool_calls: list                # 最近一轮 LLM 请求的工具调用 [ToolCall]（tools 节点消费后清空）
    tool_results: list              # 本轮工具执行结果 [ToolResult]（SSE 透传 + 可观测性）
    agent_turns: int                # ReAct 已执行轮数（agent 节点 +1，路由据此判断上限）
    tool_log: list                  # 全量工具调用轨迹（跨轮累积，供 SSE + M3 审计）
    sub_agent: dict | None          # M3 多 Agent 预留，本期恒为 None
