"""
Agent 工作流节点实现。

每个节点是一个纯函数，接收 AgentState 并返回部分 state 更新。
节点之间通过 AgentState 通信，不直接持有对方引用。
"""
from __future__ import annotations

import logging

from src.agents.state import AgentState
from src.agents.prompts import VALIDATOR_PROMPT
from src.rag.intent import classify_query_type, is_capability_query, get_capability_reply
from src.rag.engine import RAG_PROMPT_TEMPLATE, CASUAL_SYSTEM_PROMPT
from src.llm.client import Message as LLMMessage
from src.memory.token_budget import TokenBudget

logger = logging.getLogger(__name__)

# 单条历史消息截断长度（与旧行为一致）
_HISTORY_MSG_MAX_CHARS = 300
# 送入 LLM 的历史最大轮数（预算内再按 token 收紧）
_HISTORY_MAX_TURNS = 6


# ---------------------------------------------------------------------------
# 消息工具（兼容 dict 和 LangChain message 对象）
# ---------------------------------------------------------------------------

def _msg_role(m) -> str:
    if hasattr(m, "type"):
        type_map = {"human": "user", "ai": "assistant", "system": "system"}
        return type_map.get(m.type, m.type or "user")
    if isinstance(m, dict):
        return m.get("role", "user")
    return "user"


def _msg_content(m) -> str:
    if hasattr(m, "content"):
        return str(m.content) if m.content else ""
    if isinstance(m, dict):
        return str(m.get("content", ""))
    return str(m)


# ---------------------------------------------------------------------------
# 预算化上下文构建（TokenBudget）
# ---------------------------------------------------------------------------

def _fit_history(messages: list, limit_tokens: int) -> list[LLMMessage]:
    """按 token 预算从后往前挑选对话历史（上下文缩减策略的滑动窗口）。

    相比旧的"固定最近 6 轮×300 字符"，这里在轮数上限内进一步按
    chat_history 段预算收紧：消息越短能带越多轮，长消息自动少带。
    """
    msgs = list(messages or [])
    if len(msgs) > _HISTORY_MAX_TURNS * 2:
        msgs = msgs[-_HISTORY_MAX_TURNS * 2:]

    selected = []
    total = 0
    for m in reversed(msgs):
        role = _msg_role(m)
        if role not in ("human", "ai", "user", "assistant"):
            continue
        content = _msg_content(m)[:_HISTORY_MSG_MAX_CHARS]
        tokens = TokenBudget.count(content) + 4  # 角色/分隔开销
        if selected and total + tokens > limit_tokens:
            break
        total += tokens
        selected.append((role, content))

    result = []
    for role, content in reversed(selected):
        r = "user" if role == "human" else "assistant" if role == "ai" else role
        result.append(LLMMessage(r, content))
    return result


def build_budgeted_prompt(
    llm,
    template: str,
    context: str,
    query: str,
    memory_context: str = "",
    messages: list | None = None,
    extra: str = "",
) -> tuple[str, list[LLMMessage]]:
    """用 TokenBudget 按模型实际上下文窗口动态分配各段预算并组装 Prompt。

    设计（对齐"上下文缩减"策略）：
      - 窗口大小取模型真实窗口（llm.get_context_window()），不再写死 28K
      - 各段（system/记忆+条文/历史）按预算 consume，超限截断
      - 查询复杂度动态调整分配（对比查询放大检索预算等）

    Args:
        llm: LLM 实例（含 get_context_window()）
        template: 含 {context}/{query} 占位符的 RAG 模板
        context: 检索条文上下文（build_hierarchical_context 结果）
        query: 用户问题
        memory_context: 历史对话记忆上下文（注入在条文之前）
        messages: 当前会话对话历史
        extra: 追加提醒（如重试质量反馈），计入 system 段预算

    Returns:
        (prompt, history) — history 为预算内筛选的 LLMMessage 列表
    """
    window = getattr(llm, "get_context_window", lambda: 28000)()
    budget = TokenBudget(context_window=window)
    budget.adjust_for_complexity(query)

    # system 段 = 模板头部说明 + 重试提醒（计入预算，超限截断）
    head = template.split("{context}")[0]
    budget.consume("system_prompt", head + extra)

    # 记忆上下文拼在条文之前，整体计入 retrieval 预算
    if memory_context:
        context = memory_context + "\n\n" + context
    budget.consume("retrieval_docs", context)

    # 历史按 chat_history 预算筛选
    history = _fit_history(messages or [], budget.get_limit("chat_history"))

    prompt = budget.build_template(template, query)
    return prompt, history


# ---------------------------------------------------------------------------
# 上下文构建
# ---------------------------------------------------------------------------

def build_hierarchical_context(docs: list[dict]) -> str:
    """将检索结果按 (法律名, 章) 分组构建层级结构化上下文"""
    groups: dict[str, dict[str, list]] = {}
    seen = set()
    for doc in docs:
        law = doc.get("law_name", "")
        article = doc.get("article_range", "")
        key = (law, article)
        if key in seen:
            continue
        seen.add(key)
        chapter = doc.get("chapter", "") or "总则"
        groups.setdefault(law, {}).setdefault(chapter, []).append(doc)

    parts = []
    idx = 0
    for law_name, chapters in groups.items():
        for chapter, ch_docs in chapters.items():
            section = ch_docs[0].get("section", "") if ch_docs else ""
            if section:
                parts.append(f"## 《{law_name}》{chapter} → {section}")
            else:
                parts.append(f"## 《{law_name}》{chapter}")
            for doc in ch_docs:
                idx += 1
                content = doc.get("content", "")
                if "\n" in content and content.startswith("【"):
                    content = content.split("\n", 1)[1]
                parts.append(f"### {idx}. {doc.get('article_range', '')}\n{content.strip()}")

    return "\n\n".join(parts) if parts else "（未找到相关条文）"


# ---------------------------------------------------------------------------
# 节点工厂
#
# 每个节点需要访问 LLM / retriever / memory，但它们不属于 state。
# 通过闭包将外部依赖注入到节点函数中，保持节点本身无状态。
# ---------------------------------------------------------------------------

def make_nodes(llm, retriever, memory_manager, top_k: int = 5, max_retries: int = 1):
    """创建所有工作流节点（闭包注入外部依赖）

    Args:
        llm: LLMAdapter 实例
        retriever: BaseRetriever 实例
        memory_manager: ConversationMemoryManager | None
        top_k: 检索返回条数
        max_retries: 最大重试次数

    Returns:
        dict[str, callable]  节点名 → 节点函数
    """

    # ---- 意图识别 ----

    def classify_intent_node(state: AgentState) -> dict:
        query_type = classify_query_type(state["query"], history=state.get("messages", []))
        is_legal = query_type != "casual"
        logger.info(f"意图识别: '{state['query']}' → {query_type}")
        return {"is_legal_query": is_legal, "query_type": query_type}

    def route_by_intent(state: AgentState) -> str:
        return "legal" if state.get("is_legal_query", True) else "casual"

    # ---- 记忆检索 ----

    def memory_retrieve_node(state: AgentState) -> dict:
        if memory_manager is None:
            return {"memory_context": ""}
        user_id = state.get("user_id", "")
        if not user_id:
            return {"memory_context": ""}
        try:
            memories = memory_manager.retrieve(user_id, state["query"])
            context = memory_manager.build_context(memories)
            if context:
                logger.info(f"记忆命中: user={user_id[:8]}..., {len(memories)}条")
            return {"memory_context": context}
        except Exception as e:
            logger.warning(f"记忆检索失败: {e}")
            return {"memory_context": ""}

    # ---- 闲聊 ----

    def casual_reply_node(state: AgentState) -> dict:
        query = state["query"]
        # 能力问句：返回系统固定能力清单（不调 LLM，避免编造不存在的能力）
        if is_capability_query(query):
            return {"answer": get_capability_reply(), "validation_passed": True}
        answer = llm.chat(query, system_prompt=CASUAL_SYSTEM_PROMPT)
        return {"answer": answer, "validation_passed": True}

    # ---- 检索 ----

    def retrieve_node(state: AgentState) -> dict:
        q = state["query"]
        query_type = state.get("query_type", "law_lookup")

        # 按意图路由文档类型
        doc_type_map = {"case_query": "case", "law_lookup": "law"}
        doc_type = doc_type_map.get(query_type)

        docs = retriever.search(q, top_k=top_k, doc_type=doc_type)
        return {
            "retrieved_docs": [
                {"content": d.content, "law_name": d.law_name,
                 "article_range": d.article_range, "citation": d.citation,
                 "score": d.score}
                for d in docs
            ]
        }

    # ---- 生成 ----

    def generate_node(state: AgentState) -> dict:
        docs = state.get("retrieved_docs", [])
        query = state["query"]
        feedback = state.get("validation_feedback", "")
        memory_context = state.get("memory_context", "")

        ctx = build_hierarchical_context(docs)

        # 重试时追加质量提醒（计入 system 段预算）
        extra = ""
        if feedback:
            extra = f"\n\n## ⚠️ 上次回答不合格\n原因: {feedback}\n请确保本次回答: 引用法律名称、标注条款号、不编造内容。"

        # TokenBudget 预算化组装：动态窗口 + 分段截断 + 历史预算筛选
        prompt, history = build_budgeted_prompt(
            llm=llm,
            template=RAG_PROMPT_TEMPLATE,
            context=ctx,
            query=query,
            memory_context=memory_context,
            messages=state.get("messages", []),
            extra=extra,
        )

        answer = llm.chat(prompt, history=history if history else None)
        return {"answer": answer}

    # ---- 校验 ----

    def validate_node(state: AgentState) -> dict:
        answer = state.get("answer", "")
        docs = state.get("retrieved_docs", [])
        retry = state.get("retry_count", 0)
        query = state.get("query", "")

        if not docs:
            return {"validation_passed": True}

        ctx = "\n".join(
            f"- {d.get('citation','')}: {d.get('content','')[:120]}"
            for d in docs[:5]
        )
        prompt = VALIDATOR_PROMPT.format(query=query, context=ctx, answer=answer[:800])
        result = llm.chat(prompt, system_prompt="你是一个法律回答审核员。").strip()

        passed = "PASS" in result.upper()
        if not passed and retry < max_retries:
            reason = ""
            if "理由" in result:
                reason = result.split("理由", 1)[1].strip().lstrip("：:").strip()
            elif "\n" in result:
                reason = result.split("\n", 1)[1].strip()
            logger.info(f"校验未通过，重试 {retry + 1}/{max_retries}: {reason}")
            return {
                "validation_passed": False,
                "retry_count": retry + 1,
                "validation_feedback": reason or "回答未引用法律名称或条款号",
            }

        return {"validation_passed": True}

    def should_retry(state: AgentState) -> str:
        if not state.get("validation_passed", True):
            return "retry"
        return "end"

    # ---- 返回所有节点 ----

    return {
        "classify_intent": classify_intent_node,
        "route_by_intent": route_by_intent,
        "memory_retrieve": memory_retrieve_node,
        "casual_reply": casual_reply_node,
        "retrieve": retrieve_node,
        "generate": generate_node,
        "validate": validate_node,
        "should_retry": should_retry,
    }
