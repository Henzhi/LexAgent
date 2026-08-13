"""
RAG 问答引擎。

串联完整管线：查询分类 → 闲聊直回 / 检索 → 构建 Prompt → LLM 回答
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Iterator

from src.llm.client import LawLLM, Message as LLMMessage
from src.memory.token_budget import TokenBudget
from .retriever import BaseRetriever, RetrievedDoc
from .intent import needs_retrieval


# ---------------------------------------------------------------------------
# 查询分类 / 意图识别
# ---------------------------------------------------------------------------
# 意图识别相关逻辑（is_casual_query / needs_retrieval / classify_intent）已统一
# 收敛到 src/rag/intent.py，避免 engine 与 agent 各写一份导致行为不一致。

CASUAL_SYSTEM_PROMPT = """你是一位友好的法律问答助手，同时也能进行日常交流。

## 你的能力边界（只说明你实际具备的能力，绝不编造不存在的功能）
你具备的能力：
- 解答中国法律法规相关问题：查询具体法律条款、判断行为是否违法、民事责任归属等
- 支持 30+ 部常见法律（民法典、刑法、劳动法、劳动合同法、治安管理处罚法等）
- 精确引用法律名称、章节、条款号，结合上下文给出针对性解答

你不具备的能力（用户问到时请明确告知没有该能力）：
- 写代码、编程、翻译、作文、绘图、生成图片/音频/视频
- 查询实时信息、网页、天气、新闻
- 任何与法律无关的专业服务（医疗、投资、心理咨询等）

## 回复原则
- 问候类：热情简洁地回应，并简要说明你可以帮助解答法律问题
- 感谢类：礼貌回应，鼓励继续提问
- 自我介绍：说明你是基于中国法律法规的智能问答助手，可以查询 900+ 部法律、行政法规与司法解释
- 闲聊类：简短回应后，引导用户提出法律问题
- 用户问"你能做什么/你有什么功能"等能力问句：如实列出上述能力清单，并明确不存在的功能不做虚假承诺

⚠️ 请始终注意：你的回答仅供参考，不构成专业法律意见。涉及具体法律事务，建议用户咨询执业律师。

请自然友好地回复。"""


# is_casual_query / needs_retrieval / ROUTE_PROMPT 现由 src.rag.intent 提供
# （见文件顶部 `from .intent import ...`）。本模块只保留 LLM 调用与 prompt 拼接。


# ---------------------------------------------------------------------------
# 问答结果
# ---------------------------------------------------------------------------

@dataclass
class RAGAnswer:
    """一次 RAG 问答的完整结果"""
    query: str
    answer: str
    sources: list[RetrievedDoc] = field(default_factory=list)
    is_casual: bool = False

    def format_sources(self) -> str:
        """格式化引用来源"""
        if self.is_casual:
            return "  （闲聊模式，无引用）"
        lines = []
        seen = set()
        for doc in self.sources:
            key = (doc.law_name, doc.article_range)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  - {doc.citation}")
        return "\n".join(lines) if lines else "  无引用来源"


# ---------------------------------------------------------------------------
# RAG 提示词模板
# ---------------------------------------------------------------------------

RAG_PROMPT_TEMPLATE = """你是一位精通中国法律的专业法律助手。请根据以下提供的法律条文，准确回答用户的问题。

## 要求
1. **必须引用法律名称和条文编号**（如：根据《治安管理处罚法》第十条），不可只给结论不引条文
2. 基于条文内容，不编造
3. 条文不足时诚实说明
4. 回答简洁清晰
5. 回答末尾固定附加以下免责声明：
   ⚠️ 以上内容基于现行法律法规整理，仅供参考，不构成专业法律意见。如涉及具体法律事务，请咨询执业律师。

## 示例
用户: 治安处罚有哪些种类？
条文: 第十条: 治安管理处罚的种类分为：(一)警告；(二)罚款；(三)行政拘留；(四)吊销公安机关发放的许可证。
回答: 根据治安管理处罚法第十条，治安处罚共有四种：警告、罚款、行政拘留、吊销公安机关发放的许可证。

⚠️ 以上内容基于现行法律法规整理，仅供参考，不构成专业法律意见。如涉及具体法律事务，请咨询执业律师。

用户: 酒驾怎么处罚？
条文: 第九十一条: 饮酒后驾驶机动车的，处暂扣六个月机动车驾驶证，并处一千元以上二千元以下罚款。
回答: 根据道路交通安全法第九十一条，饮酒驾驶机动车，处暂扣六个月驾驶证，并处一千元以上二千元以下罚款。

⚠️ 以上内容基于现行法律法规整理，仅供参考，不构成专业法律意见。如涉及具体法律事务，请咨询执业律师。

---

## 相关法律条文
{context}

---

## 用户问题
{query}

---"""


# ---------------------------------------------------------------------------
# RAG 引擎
# ---------------------------------------------------------------------------

class RAGEngine:
    """RAG 问答引擎：检索 + LLM 回答"""

    def __init__(
        self,
        retriever: BaseRetriever,
        llm: LawLLM,
        top_k: int = 5,
        prompt_template: str = RAG_PROMPT_TEMPLATE,
        query_logger=None,  # QueryLogger | None
    ):
        """
        Args:
            retriever: 检索器（pgvector）
            llm: LLM 客户端
            top_k: 每次检索返回的文档数
            prompt_template: 自定义提示词模板
            query_logger: 检索质量日志记录器（可观测性）
        """
        self.retriever = retriever
        self.llm = llm
        self.top_k = top_k
        self.prompt_template = prompt_template
        self._qlog = query_logger

    # ------------------------------------------------------------------
    # 问答
    # ------------------------------------------------------------------

    def ask(self, query: str) -> RAGAnswer:
        """单次问答，LLM 自省路由：闲聊直回 / 法律RAG"""
        qlog = self._qlog
        with (qlog.trace("", query) if qlog else nullcontext()) as trace:
            t0 = time.time()
            need_retrieval = needs_retrieval(query, self.llm)
            if trace is not None:
                trace.set_intent("casual" if not need_retrieval else "law_lookup")
                trace.stage("intent", int((time.time() - t0) * 1000))

            # LLM 自省：是否需要检索？
            if not need_retrieval:
                answer = self.llm.chat(query, system_prompt=CASUAL_SYSTEM_PROMPT)
                if trace is not None:
                    trace.finalize(faq_cache_hit=False, retrieved_count=0)
                return RAGAnswer(query=query, answer=answer, is_casual=True)

            # 法律 RAG
            t1 = time.time()
            docs = self.retriever.search(query, top_k=self.top_k)
            if trace is not None:
                trace.stage("retrieve", int((time.time() - t1) * 1000))
            prompt = self._build_prompt(query, docs)
            t2 = time.time()
            answer = self.llm.chat(prompt)
            if trace is not None:
                trace.stage("generate", int((time.time() - t2) * 1000))
                trace.finalize(retrieved_count=len(docs), reranked_count=0, faq_cache_hit=False)
            return RAGAnswer(query=query, answer=answer, sources=docs)

    def ask_stream(self, query: str) -> Iterator[str]:
        """流式问答，LLM 自省路由"""
        qlog = self._qlog
        with (qlog.trace("", query) if qlog else nullcontext()) as trace:
            t0 = time.time()
            need_retrieval = needs_retrieval(query, self.llm)
            if trace is not None:
                trace.set_intent("casual" if not need_retrieval else "law_lookup")
                trace.stage("intent", int((time.time() - t0) * 1000))
            if not need_retrieval:
                yield from self.llm.chat_stream(query, system_prompt=CASUAL_SYSTEM_PROMPT)
                if trace is not None:
                    trace.finalize(faq_cache_hit=False, retrieved_count=0)
                return

            t1 = time.time()
            docs = self.retriever.search(query, top_k=self.top_k)
            if trace is not None:
                trace.stage("retrieve", int((time.time() - t1) * 1000))
            prompt = self._build_prompt(query, docs)
            yield from self.llm.chat_stream(prompt)
            if trace is not None:
                trace.finalize(retrieved_count=len(docs), reranked_count=0, faq_cache_hit=False)

    # ------------------------------------------------------------------
    # 多轮对话
    # ------------------------------------------------------------------

    def chat(
        self,
        query: str,
        history: list[LLMMessage] | None = None,
    ) -> RAGAnswer:
        """多轮对话（带历史，每次重新检索）

        Args:
            query: 当前问题
            history: 历史消息

        Returns:
            RAGAnswer
        """
        qlog = self._qlog
        with (qlog.trace("", query) if qlog else nullcontext()) as trace:
            t0 = time.time()
            docs = self.retriever.search(query, top_k=self.top_k)
            if trace is not None:
                trace.set_intent("law_lookup")
                trace.stage("retrieve", int((time.time() - t0) * 1000))
            prompt = self._build_prompt(query, docs)

            history = history or []
            t1 = time.time()
            answer = self.llm.chat(prompt, history=history)
            if trace is not None:
                trace.stage("generate", int((time.time() - t1) * 1000))
                trace.finalize(retrieved_count=len(docs), reranked_count=0, faq_cache_hit=False)

            return RAGAnswer(query=query, answer=answer, sources=docs)

    def chat_stream(
        self,
        query: str,
        history: list[LLMMessage] | None = None,
    ) -> Iterator[str]:
        """多轮流式对话"""
        qlog = self._qlog
        with (qlog.trace("", query) if qlog else nullcontext()) as trace:
            t0 = time.time()
            docs = self.retriever.search(query, top_k=self.top_k)
            if trace is not None:
                trace.set_intent("law_lookup")
                trace.stage("retrieve", int((time.time() - t0) * 1000))
            prompt = self._build_prompt(query, docs)
            history = history or []
            yield from self.llm.chat_stream(prompt, history=history)
            if trace is not None:
                trace.finalize(retrieved_count=len(docs), reranked_count=0, faq_cache_hit=False)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_prompt(self, query: str, docs: list[RetrievedDoc]) -> str:
        """将检索结果格式化为 prompt 中的上下文，按法律+章节分组"""
        # 按 (法律名, 章) 分组，保留层次结构
        groups: dict[str, dict[str, list]] = {}  # law_name → chapter → [docs]
        seen = set()
        for doc in docs:
            key = (doc.law_name, doc.article_range)
            if key in seen:
                continue
            seen.add(key)
            chapter = doc.chapter or "总则"
            groups.setdefault(doc.law_name, {}).setdefault(chapter, []).append(doc)

        # 构建分组上下文
        parts = []
        idx = 0
        for law_name, chapters in groups.items():
            for chapter, ch_docs in chapters.items():
                # 章节头
                section = ch_docs[0].section if ch_docs and ch_docs[0].section else ""
                if section:
                    parts.append(f"## 《{law_name}》{chapter} → {section}")
                else:
                    parts.append(f"## 《{law_name}》{chapter}")
                for doc in ch_docs:
                    idx += 1
                    content = self._extract_core(doc)
                    parts.append(f"### {idx}. {doc.article_range}\n{content}")

        context = "\n\n".join(parts) if parts else "（未找到相关条文）"

        # TokenBudget 预算截断：按模型真实窗口动态分配检索段预算，超限截断
        window = getattr(self.llm, "get_context_window", lambda: 28000)()
        budget = TokenBudget(context_window=window)
        budget.adjust_for_complexity(query)
        budget.consume("retrieval_docs", context)
        context = budget._segments.get("retrieval_docs", context)

        return self.prompt_template.format(context=context, query=query)

    @staticmethod
    def _extract_core(doc: RetrievedDoc) -> str:
        """从 chunk 内容中提取核心文本（去掉前缀元数据）"""
        content = doc.content
        # chapter_summary chunk 的格式是 "【法律名】／章\n第一条: xxx\n第二条: xxx"
        # article chunk 的格式是 "【法律名】／章\n条文内容"
        # 去掉第一行前缀，保留实质内容
        if "\n" in content and content.startswith("【"):
            content = content.split("\n", 1)[1]
        return content.strip()
