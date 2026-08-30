"""
retrieve_knowledge 内置工具（M1 / F1）。

封装 `BaseRetriever.search(query, top_k, doc_type)` 为 Agent 可调用工具：
- schema 含 query / doc_type / top_k，参数少、required 明确（降低小模型调用失败率，R3）；
- 内部库检索结果为最高优先级法律依据（REQ-U3），source=internal_kb；
- 检索异常不抛出，返回 ToolResult(ok=False)（summary 首词"检索失败"）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from src.agents.tools.base import (
    CATEGORY_KNOWLEDGE,
    SOURCE_INTERNAL_KB,
    ToolResult,
    ToolSpec,
    tool,
    truncate_summary,
)
from src.rag.retriever import BaseRetriever, RetrievedDoc

logger = logging.getLogger(__name__)

# 单次工具结果摘要最多展示的条文数（防止 summary 超过 300 字符上限）
_SUMMARY_MAX_DOCS = 3
# 单条条文在 summary 中的内容截断长度
_SUMMARY_DOC_CONTENT_CHARS = 60


def _doc_to_dict(doc: RetrievedDoc) -> dict[str, Any]:
    """RetrievedDoc → dict（供 ToolResult.data.docs 与 sources 使用）。"""
    return {
        "content": doc.content,
        "score": float(doc.score),
        "law_name": doc.law_name,
        "chapter": doc.chapter,
        "section": doc.section,
        "article_range": doc.article_range,
        "chunk_type": doc.chunk_type,
        "citation": doc.citation,
    }


def _build_summary(docs: list[RetrievedDoc]) -> str:
    """构建摘要：检索命中数 + 前几条（法名+条号+内容片段）。"""
    if not docs:
        return "内部知识库未检索到相关法条"
    parts = [f"检索到 {len(docs)} 条相关法条："]
    for i, d in enumerate(docs[:_SUMMARY_MAX_DOCS], start=1):
        content = d.content.replace("\n", " ").strip()
        if len(content) > _SUMMARY_DOC_CONTENT_CHARS:
            content = content[:_SUMMARY_DOC_CONTENT_CHARS] + "…"
        parts.append(f"{i}) {d.citation}：{content}")
    return truncate_summary("；".join(parts))


def build_retrieve_knowledge_spec(
    retriever: BaseRetriever,
    default_top_k: int = 5,
) -> ToolSpec:
    """构造 retrieve_knowledge 工具的 ToolSpec（依赖经闭包注入）。

    Args:
        retriever: 统一检索入口（pgvector → Reranker → AdjacentExpander → Hybrid → ArticleRouter 链）
        default_top_k: LLM 未指定 top_k 时的默认返回条数
    """

    @tool(name="retrieve_knowledge", category=CATEGORY_KNOWLEDGE)
    def retrieve_knowledge(
        query: Annotated[str, "法律检索查询语句，建议使用规范法言法语，如《治安管理处罚法》行政拘留"],
        doc_type: Annotated[
            Literal["law", "case"] | None,
            "文档类型：law=法条，case=案例；不传则不限",
        ] = None,
        top_k: Annotated[int, "返回结果条数，默认 5，最大 20"] = 5,
    ) -> ToolResult:
        """从系统内部法律知识库检索相关法条或案例。

        当需要引用具体法条原文、查询法律条文规定或查找类案时优先调用本工具；
        内部库检索结果为最高优先级法律依据。
        """
        try:
            k = int(top_k) if top_k else default_top_k
            k = max(1, min(k, 20))
            docs = retriever.search(query, top_k=k, doc_type=doc_type)
            return ToolResult(
                tool="retrieve_knowledge",
                call_id="",
                ok=True,
                summary=_build_summary(docs),
                data={
                    "docs": [_doc_to_dict(d) for d in docs],
                    "count": len(docs),
                },
                source=SOURCE_INTERNAL_KB,
            )
        except Exception as e:
            logger.error(f"retrieve_knowledge 检索失败: query={query} doc_type={doc_type}", exc_info=True)
            return ToolResult(
                tool="retrieve_knowledge",
                call_id="",
                ok=False,
                summary=f"检索失败: {e}",
                data={},
                source=SOURCE_INTERNAL_KB,
            )

    return retrieve_knowledge
