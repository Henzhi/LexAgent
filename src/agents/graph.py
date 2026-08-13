"""
LangGraph 多 Agent 工作流编排 (v0.6 / M1)。

职责单一：构建和编译 StateGraph，将节点连接成工作流。
节点实现 → agents/nodes.py（固定管线）| agents/react_nodes.py（ReAct）
状态定义 → agents/state.py
提示词   → agents/prompts.py

固定管线（AGENT_REACT_ENABLED=false 或主后端降级）:
    intent → FAQ缓存检查 → memory_retrieve → retrieve → generate → validate
ReAct 管线（AGENT_ENABLED=true + AGENT_REACT_ENABLED=true，M1 默认）:
    intent → memory_retrieve → [agent ⇄ tools] → validate → END（校验失败走 generate 兜底）
    其中固定 retrieve 节点移除，由 LLM 自主决定调用 retrieve_knowledge 工具。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from langgraph.graph import StateGraph, END

from src.config import AGENT_MAX_TOOL_TURNS, AGENT_REACT_ENABLED
from src.agents.state import AgentState
from src.agents.nodes import make_nodes, build_hierarchical_context, build_budgeted_prompt
from src.agents.react_nodes import make_react_nodes
from src.agents.tools import ToolRegistry, build_default_tools
from src.rag.retriever import BaseRetriever
from src.rag.engine import RAG_PROMPT_TEMPLATE
from src.rag.intent import classify_query_type, is_capability_query, get_capability_reply
from src.memory.hallucination_guard import HallucinationGuard

logger = logging.getLogger(__name__)


@contextmanager
def _null_trace():
    """无 QueryLogger 时的 no-op 追踪上下文（yield None）"""
    yield None


def _backend_degraded(llm) -> bool:
    """判断 LLM 后端是否已降级（FailoverLLMBackend 专用；普通后端恒为 False）。"""
    backend = getattr(llm, "_backend", None) or llm
    return bool(getattr(backend, "degraded", False))


def _supports_tools(llm) -> bool:
    """LLM 是否具备工具调用能力（实现 chat_with_tools）。

    兼容旧的自定义 LLM 包装（仅实现 chat/chat_stream）：不具备工具调用能力时
    回退固定管线（Q3：模型不支持工具 → 固定检索+直接生成），保证 AC-7。
    """
    return hasattr(llm, "chat_with_tools") or hasattr(getattr(llm, "_backend", None), "chat_with_tools")


def _chunk_text(text: str, size: int = 16) -> Iterator[str]:
    """将长文本按块切分（SSE 分块推送模拟打字机效果，D2）。"""
    for i in range(0, len(text), size):
        yield text[i:i + size]


class LawAgentGraph:
    """LangGraph 多 Agent 法律问答引擎

    用法:
        agent = LawAgentGraph(retriever, llm)
        for token in agent.stream("行政拘留最长多久", history=[]):
            print(token, end="")
    """

    def __init__(
        self,
        retriever: BaseRetriever,
        llm,                    # LLMAdapter
        top_k: int = 5,
        max_retries: int = 1,
        memory_manager = None,  # ConversationMemoryManager | None
        faq_cache = None,       # FAQCache | None
        query_logger = None,    # QueryLogger | None
        registry: ToolRegistry | None = None,  # M1：工具注册表（默认注册内置工具）
    ):
        self.retriever = retriever
        self.llm = llm
        self.top_k = top_k
        self.max_retries = max_retries
        self._memory = memory_manager
        self._faq_cache = faq_cache
        self._qlog = query_logger
        self.registry = registry or build_default_tools(retriever)

        # 通过工厂函数注入依赖，节点本身无状态
        nodes = make_nodes(llm, retriever, memory_manager, top_k, max_retries)
        self._nodes = nodes

        # M1：ReAct 图开关 —— AGENT_REACT_ENABLED=true 且主后端未降级（Ollama 降级 → 固定管线）
        # 且 LLM 具备工具调用能力（chat_with_tools）；任一不满足 → 固定管线（AC-7）
        self._react_enabled = (
            AGENT_REACT_ENABLED
            and not _backend_degraded(llm)
            and _supports_tools(llm)
        )
        if self._react_enabled:
            self._react = make_react_nodes(
                llm, self.registry,
                max_tool_turns=AGENT_MAX_TOOL_TURNS,
            )
            self._graph = self._build_react_graph(nodes, self._react)
            logger.info("Agent 图构建: ReAct 工具调用模式 (max_tool_turns=%d)", AGENT_MAX_TOOL_TURNS)
        else:
            self._react = None
            self._graph = self._build_graph(nodes)
            if not AGENT_REACT_ENABLED:
                reason = "AGENT_REACT_ENABLED=false"
            elif _backend_degraded(llm):
                reason = "主后端已降级（Ollama）"
            else:
                reason = "LLM 不具备工具调用能力"
            logger.info("Agent 图构建: 固定管线模式 (%s)", reason)

    # ------------------------------------------------------------------
    # 图构建
    # ------------------------------------------------------------------

    def _build_graph(self, nodes: dict) -> StateGraph:
        """固定管线图（AC-7 向后兼容路径）。"""
        builder = StateGraph(AgentState)

        builder.add_node("intent", nodes["classify_intent"])
        builder.add_node("casual_reply", nodes["casual_reply"])
        builder.add_node("memory_retrieve", nodes["memory_retrieve"])
        builder.add_node("retrieve", nodes["retrieve"])
        builder.add_node("generate", nodes["generate"])
        builder.add_node("validate", nodes["validate"])

        builder.set_entry_point("intent")
        builder.add_conditional_edges(
            "intent", nodes["route_by_intent"],
            {"legal": "memory_retrieve", "casual": "casual_reply"},
        )
        builder.add_edge("casual_reply", END)
        builder.add_edge("memory_retrieve", "retrieve")
        builder.add_edge("retrieve", "generate")
        builder.add_conditional_edges(
            "validate", nodes["should_retry"],
            {"retry": "generate", "end": END},
        )
        builder.add_edge("generate", "validate")

        return builder.compile()

    def _build_react_graph(self, nodes: dict, react: dict) -> StateGraph:
        """ReAct 图：intent → memory_retrieve → [agent ⇄ tools] → validate → END。

        - agent → tools（有 tool_calls）| validate（无 tool_calls / 达轮数上限强制产出）
        - tools → agent（继续循环）
        - validate → generate（FAIL 时固定生成节点兜底，复用原重试语义）
        """
        builder = StateGraph(AgentState)

        builder.add_node("intent", nodes["classify_intent"])
        builder.add_node("casual_reply", nodes["casual_reply"])
        builder.add_node("memory_retrieve", nodes["memory_retrieve"])
        builder.add_node("agent", react["agent"])
        builder.add_node("tools", react["tools"])
        builder.add_node("generate", nodes["generate"])
        builder.add_node("validate", nodes["validate"])

        builder.set_entry_point("intent")
        builder.add_conditional_edges(
            "intent", nodes["route_by_intent"],
            {"legal": "memory_retrieve", "casual": "casual_reply"},
        )
        builder.add_edge("casual_reply", END)
        builder.add_edge("memory_retrieve", "agent")
        builder.add_conditional_edges(
            "agent", react["route_after_agent"],
            {"tools": "tools", "final": "validate"},
        )
        builder.add_edge("tools", "agent")
        builder.add_conditional_edges(
            "validate", nodes["should_retry"],
            {"retry": "generate", "end": END},
        )
        builder.add_edge("generate", "validate")

        return builder.compile()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def ask(self, query: str, history: list[dict] | None = None, user_id: str = "") -> dict:
        """同步问答 — 含 FAQ 缓存检查（与 stream() 路径行为一致）"""
        qlog = self._qlog
        with (qlog.trace(user_id or "", query) if qlog else _null_trace()) as trace:
            t0 = time.time()
            query_type = classify_query_type(query, history=history or [])
            if trace is not None:
                trace.set_intent(query_type)
                trace.stage("intent", int((time.time() - t0) * 1000))

            # FAQ 缓存检查
            if self._faq_cache:
                t1 = time.time()
                try:
                    cached = self._faq_cache.check(query)
                    if trace is not None:
                        trace.stage("faq", int((time.time() - t1) * 1000))
                    if cached:
                        logger.info(f"ask() FAQ缓存命中: score={cached['score']}")
                        if trace is not None:
                            trace.finalize(faq_cache_hit=True, retrieved_count=0)
                        return {
                            "query": query, "answer": cached["answer"],
                            "retrieved_docs": [], "is_legal_query": True,
                            "cached": True, "tool_log": [],
                        }
                except Exception as e:
                    logger.warning(f"FAQ缓存检查失败: {e}")

            initial: AgentState = {
                "query": query,
                "messages": history or [],
                "retrieved_docs": [],
                "answer": "",
                "validation_passed": False,
                "validation_feedback": "",
                "retry_count": 0,
                "is_legal_query": query_type != "casual",
                "query_type": query_type,
                "memory_context": "",
                "user_id": user_id,
                "tool_calls": [],
                "tool_results": [],
                "agent_turns": 0,
                "tool_log": [],
                "sub_agent": None,
            }
            t2 = time.time()
            result = self._graph.invoke(initial)
            if trace is not None:
                trace.stage("agent", int((time.time() - t2) * 1000))

            # 幻觉防御（ask() 路径与 stream() 行为对齐）
            # ReAct 模式下未触发内部检索属正常决策（LLM 可直接作答/仅用网络线索），
            # 跳过"空文档即拦截"的 Layer 1 判断；有检索结果时正常走完整守卫。
            if query_type != "casual":
                react_skipped_retrieval = self._react_enabled and not result.get("retrieved_docs")
                if not react_skipped_retrieval:
                    guard_result = HallucinationGuard.guard(
                        result.get("retrieved_docs", []),
                        result.get("answer", ""),
                    )
                    if guard_result["blocked"]:
                        logger.warning(f"ask() 回答被拦截: {guard_result['reason']}")
                        result["answer"] = guard_result["fallback"]

            # M1：同步路径附带工具调用轨迹与降级标记（共享约定 §8.6 / AC-6）
            result["tool_log"] = result.get("tool_log", [])
            result["degraded"] = _backend_degraded(self.llm)

            if trace is not None:
                trace.finalize(
                    retrieved_count=len(result.get("retrieved_docs", [])),
                    reranked_count=0,
                    faq_cache_hit=False,
                    memory_docs_used=1 if result.get("memory_context") else 0,
                    llm_tokens=0,
                )
            return result

    def stream(self, query: str, history: list[dict] | None = None, user_id: str = "") -> Iterator[dict]:
        """流式问答 - 手动步进 + LLM 真实流式输出（含可观测性埋点）"""
        trace = None
        if self._qlog:
            trace = self._qlog.start(user_id or "", query)
        try:
            yield {"type": "thinking", "content": "🔧 正在初始化 Agent..."}
            if _backend_degraded(self.llm):
                yield {"type": "thinking", "content": "⚠️ 当前使用降级模型（Ollama），部分能力受限（AC-6）"}

            # 1. 意图识别（结合对话历史判断延续性闲聊）
            t0 = time.time()
            query_type = classify_query_type(query, history=history or [])
            if trace is not None:
                trace.set_intent(query_type)
                trace.stage("intent", int((time.time() - t0) * 1000))
            is_legal = query_type != "casual"
            type_label = {"law_lookup": "法律条文查询", "case_query": "案例检索", "casual": "闲聊"}
            yield {"type": "thinking", "content": f"🎯 意图识别: {type_label.get(query_type, query_type)}"}

            if not is_legal:
                yield {"type": "thinking", "content": "📝 直接回复，无需检索"}
                # 能力问句 → 固定能力清单（不调 LLM，避免编造系统不具备的能力）
                if is_capability_query(query):
                    yield {"type": "token", "content": get_capability_reply()}
                    yield {"type": "thinking", "content": "✅ 完成"}
                    if trace is not None:
                        trace.finalize(faq_cache_hit=False, retrieved_count=0)
                    return
                # 闲聊也带上历史，让 LLM 结合上下文（如自我介绍后问"我是谁"）
                for token in self.llm.chat_stream(query, history=history or []):
                    yield {"type": "token", "content": token}
                yield {"type": "thinking", "content": "✅ 完成"}
                if trace is not None:
                    trace.finalize(faq_cache_hit=False, retrieved_count=0)
                return

            # 2. FAQ 缓存检查（命中则直接返回，未命中继续 RAG 流程）
            if self._faq_cache:
                t1 = time.time()
                yield {"type": "thinking", "content": "⚡ 检查 FAQ 缓存..."}
                try:
                    cached = self._faq_cache.check(query)
                    if trace is not None:
                        trace.stage("faq", int((time.time() - t1) * 1000))
                    if cached:
                        yield {"type": "FAQ", "content": f"⚡ FAQ 缓存命中 (相似度: {cached['score']:.3f})"}
                        yield {"type": "token", "content": cached["answer"]}
                        yield {"type": "meta", "sources": cached.get("sources", []), "is_casual": False, "cache_hit": True}
                        yield {"type": "thinking", "content": "✅ 完成（来自缓存）"}
                        if trace is not None:
                            trace.finalize(faq_cache_hit=True, retrieved_count=0)
                        return
                except Exception as e:
                    logger.warning(f"FAQ缓存检查失败: {e}")

            state: dict = {
                "query": query, "messages": history or [],
                "retrieved_docs": [], "answer": "", "validation_passed": False,
                "retry_count": 0, "validation_feedback": "", "is_legal_query": True,
                "query_type": query_type, "memory_context": "", "user_id": user_id,
                "tool_calls": [], "tool_results": [], "agent_turns": 0,
                "tool_log": [], "sub_agent": None,
            }

            # 3. 记忆检索
            memory_count = 0
            if self._memory and user_id:
                t2 = time.time()
                yield {"type": "thinking", "content": "🧠 检索历史记忆..."}
                try:
                    memories = self._memory.retrieve(user_id, query)
                    if trace is not None:
                        trace.stage("memory", int((time.time() - t2) * 1000))
                    ctx = self._memory.build_context(memories)
                    if ctx:
                        state["memory_context"] = ctx
                        memory_count = len(memories)
                        yield {"type": "thinking", "content": f"🧠 找到 {len(memories)} 条相关历史记忆"}
                except Exception as e:
                    logger.warning(f"流式: 记忆检索失败: {e}")

            # 4. ReAct 模式（M1）：agent⇄tools 循环 + SSE tool_call/tool_result 透传
            if self._react_enabled:
                yield from self._stream_react(state, trace, query, query_type, memory_count)
                return

            # 5. 固定管线（AC-7 向后兼容路径）
            # 检索结果在整个重试循环内复用：校验不通过是回答质量问题，检索结果
            # 没有变化，重试不应重新检索、更不应把检索到的全部条文再次推给前端。
            cached_docs: list | None = None
            sources: list = []

            for attempt in range(self.max_retries + 1):
                if attempt > 0:
                    yield {"type": "clear", "content": ""}
                    yield {"type": "thinking", "content": f"--- 第 {attempt + 1} 次尝试 ---"}

                # 4. Retrieve（仅首次尝试执行并推送 sources）
                if cached_docs is None:
                    t3 = time.time()
                    yield {"type": "thinking", "content": "🔍 正在检索法律条文..."}
                    state.update(self._nodes["retrieve"](state))
                    if trace is not None:
                        trace.stage("retrieve", int((time.time() - t3) * 1000))
                    docs = state.get("retrieved_docs", [])
                    type_hint = "案例" if query_type == "case_query" else "条文"
                    yield {"type": "thinking", "content": f"📚 检索完成，找到 {len(docs)} 条相关{type_hint}"}
                    if docs:
                        citations = [d.get("citation", "") for d in docs[:5]]
                        yield {"type": "thinking", "content": f"📖 引用: {', '.join(citations)}"}
                    cached_docs = docs
                    sources = [
                        {
                            "law_name": d.get("law_name", ""),
                            "chapter": d.get("chapter", ""),
                            "section": d.get("section", ""),
                            "article_range": d.get("article_range", ""),
                            "citation": d.get("citation", ""),
                            "score": 0.0,
                            "content": d.get("content", ""),
                        }
                        for d in docs
                    ]
                    yield {"type": "meta", "sources": sources, "is_casual": False}
                else:
                    # 重试：复用首次检索结果（不再推送 sources）
                    docs = cached_docs
                    state["retrieved_docs"] = docs

                # 5. Generate
                t4 = time.time()
                yield {"type": "thinking", "content": "💭 模型正在思考..."}
                fb = state.get("validation_feedback", "")
                memory_ctx = state.get("memory_context", "")
                ctx = build_hierarchical_context(docs)
                extra = f"\n\n## ⚠️ 上次回答不合格\n原因: {fb}\n请确保本次回答: 引用法律名称、标注条款号、不编造内容。" if fb else ""

                # TokenBudget 预算化组装：动态窗口 + 分段截断 + 历史预算筛选
                prompt, hist = build_budgeted_prompt(
                    llm=self.llm,
                    template=RAG_PROMPT_TEMPLATE,
                    context=ctx,
                    query=query,
                    memory_context=memory_ctx,
                    messages=state.get("messages", []),
                    extra=extra,
                )

                answer_raw = ""
                try:
                    stream = self.llm.chat_stream(prompt, history=hist if hist else None)
                    for token in stream:
                        yield {"type": "token", "content": token}
                        answer_raw += token
                except Exception as e:
                    logger.error(f"流式生成异常: {e}", exc_info=True)
                    answer_raw = ""
                if trace is not None:
                    trace.stage("generate", int((time.time() - t4) * 1000))
                state["answer"] = answer_raw.strip() or "(未能生成回答)"

                # 6. Validate
                t5 = time.time()
                yield {"type": "thinking", "content": "🔎 审核回答质量..."}
                state.update(self._nodes["validate"](state))
                if trace is not None:
                    trace.stage("validate", int((time.time() - t5) * 1000))
                if state.get("validation_passed", True):
                    yield {"type": "thinking", "content": "✅ 审核通过"}
                    # 幻觉防御：检索置信度 + 内容安全
                    guard_result = HallucinationGuard.guard(docs, state["answer"])
                    if guard_result["blocked"]:
                        yield {"type": "clear", "content": ""}
                        yield {"type": "token", "content": guard_result["fallback"]}
                        yield {"type": "thinking", "content": f"⚠️ 回答已拦截: {guard_result['reason']}"}
                        if trace is not None:
                            trace.finalize(faq_cache_hit=False, retrieved_count=len(docs))
                        return
                    # 校验通过 → 存入 FAQ 缓存
                    if self._faq_cache:
                        try:
                            related_laws = list(set(d.get("law_name", "") for d in docs if d.get("law_name")))
                            self._faq_cache.store(
                                question=query,
                                answer=state["answer"],
                                sources=sources,
                                related_laws=related_laws,
                                confidence=0.9,
                            )
                        except Exception as e:
                            logger.warning(f"FAQ缓存写入失败: {e}")
                    break
                fb = state.get("validation_feedback", "")
                yield {"type": "thinking", "content": f"❌ 未通过{f': {fb}' if fb else ''}，重新生成..."}

            yield {"type": "thinking", "content": "✅ 全部完成"}
            if trace is not None:
                trace.finalize(
                    retrieved_count=len(docs),
                    reranked_count=0,
                    faq_cache_hit=False,
                    memory_docs_used=memory_count,
                    llm_tokens=0,
                )
        finally:
            if trace is not None and not trace._finalized:
                trace._save()

    # ------------------------------------------------------------------
    # ReAct 流式路径（M1）
    # ------------------------------------------------------------------

    def _stream_react(
        self,
        state: dict,
        trace,
        query: str,
        query_type: str,
        memory_count: int,
    ) -> Iterator[dict]:
        """手动步进 ReAct 循环并产出 SSE 事件（tool_call/tool_result/token/meta）。

        与固定管线共用 validate/generate 兜底节点与幻觉守卫，保证行为一致性。
        """
        react = self._react
        yield {"type": "thinking", "content": "🤖 进入 Agent 工具调用模式"}

        # ---- ReAct 循环：agent ⇄ tools ----
        # 终止性由 agent_node 保证：达到轮数上限时移除 tools，模型被迫产出最终答案（REQ-UW4）。
        max_turns = AGENT_MAX_TOOL_TURNS
        guard = 0
        while True:
            guard += 1
            if guard > max_turns + 2:
                logger.warning("ReAct 循环超出安全上限，强制终止")
                break
            upd = react["agent"](state)
            state.update(upd)
            tool_calls = state.get("tool_calls", []) or []
            if not tool_calls:
                break
            turn = state.get("agent_turns", 0) or 0
            # SSE: LLM 决策调用工具（F4）
            for tc in tool_calls:
                yield {
                    "type": "tool_call",
                    "tool": tc.name,
                    "arguments": tc.arguments,
                    "turn": turn,
                }
            # 执行全部 tool_calls（DeepSeek V4 parallel_tool_calls 恒启用，R5）
            toup = react["tools"](state)
            state.update(toup)
            # SSE: 工具执行结果（F4，summary 已截断 ≤300 字符）
            for res in state.get("tool_results", []) or []:
                yield {
                    "type": "tool_result",
                    "tool": res.tool,
                    "ok": res.ok,
                    "summary": res.summary,
                    "turn": turn,
                }

        answer = (state.get("answer", "") or "").strip() or "抱歉，暂时无法回答该问题。"
        state["answer"] = answer
        docs = state.get("retrieved_docs", []) or []

        # ---- 校验（FAIL → 固定生成节点兜底，保留原重试语义）----
        yield {"type": "thinking", "content": "🔎 审核回答质量..."}
        validated = False
        for _attempt in range(self.max_retries + 1):
            state.update(self._nodes["validate"](state))
            if state.get("validation_passed", True):
                validated = True
                break
            fb = state.get("validation_feedback", "")
            yield {"type": "clear", "content": ""}
            yield {"type": "thinking", "content": f"❌ 未通过{f': {fb}' if fb else ''}，重新生成..."}
            state.update(self._nodes["generate"](state))
            answer = (state.get("answer", "") or "").strip() or "抱歉，暂时无法回答该问题。"
            state["answer"] = answer
        yield {"type": "thinking", "content": "✅ 审核通过" if validated else "⚠️ 审核未完全通过，已尽力生成"}

        # ---- 幻觉防御 ----
        # ReAct 模式未触发内部检索（docs 为空）属正常决策：跳过"空文档即拦截"的 Layer 1
        if docs:
            guard_result = HallucinationGuard.guard(docs, state["answer"])
            if guard_result["blocked"]:
                yield {"type": "clear", "content": ""}
                state["answer"] = guard_result["fallback"]
                yield {"type": "thinking", "content": f"⚠️ 回答已拦截: {guard_result['reason']}"}

        # ---- sources（内部库检索结果，来源标注）----
        sources = [
            {
                "law_name": d.get("law_name", ""),
                "chapter": d.get("chapter", ""),
                "section": d.get("section", ""),
                "article_range": d.get("article_range", ""),
                "citation": d.get("citation", ""),
                "score": float(d.get("score", 0.0) or 0.0),
                "content": d.get("content", ""),
            }
            for d in docs
        ]
        yield {"type": "meta", "sources": sources, "is_casual": False}

        # ---- 最终答案分块推送（D2：非流式决策 + SSE 分块模拟流式）----
        yield {"type": "thinking", "content": "📝 正在输出回答..."}
        for chunk in _chunk_text(state["answer"]):
            yield {"type": "token", "content": chunk}

        # ---- FAQ 缓存 ----
        if self._faq_cache:
            try:
                related_laws = list(set(d.get("law_name", "") for d in docs if d.get("law_name")))
                self._faq_cache.store(
                    question=query,
                    answer=state["answer"],
                    sources=sources,
                    related_laws=related_laws,
                    confidence=0.9,
                )
            except Exception as e:
                logger.warning(f"FAQ缓存写入失败: {e}")

        yield {"type": "thinking", "content": "✅ 全部完成"}
        if trace is not None:
            trace.finalize(
                retrieved_count=len(docs),
                reranked_count=0,
                faq_cache_hit=False,
                memory_docs_used=memory_count,
                llm_tokens=0,
            )
