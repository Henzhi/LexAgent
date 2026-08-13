"""
工具包导出（M1 / F1）。

`build_default_tools(retriever)` 是便捷工厂：注册 retrieve_knowledge + web_search 两个内置工具。
"""
from __future__ import annotations

from src.agents.tools.base import (
    CATEGORY_KNOWLEDGE,
    CATEGORY_WEB,
    SOURCE_INTERNAL_KB,
    SOURCE_LEGAL,
    SOURCE_WEB,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    truncate_summary,
)
from src.agents.tools.registry import ToolRegistry
from src.agents.tools.retrieve_knowledge import RetrieveKnowledgeTool
from src.agents.tools.web_search import WebSearchTool
from src.config import TAVILY_API_KEY, TAVILY_MAX_RESULTS, TAVILY_TIMEOUT
from src.rag.retriever import BaseRetriever
from src.search.tavily import TavilySearchClient

__all__ = [
    "ToolSpec",
    "ToolResult",
    "ToolExecutionError",
    "ToolRegistry",
    "RetrieveKnowledgeTool",
    "WebSearchTool",
    "build_default_tools",
    "CATEGORY_KNOWLEDGE",
    "CATEGORY_WEB",
    "SOURCE_INTERNAL_KB",
    "SOURCE_WEB",
    "SOURCE_LEGAL",
    "truncate_summary",
]


def build_default_tools(
    retriever: BaseRetriever,
    tavily_client: TavilySearchClient | None = None,
) -> ToolRegistry:
    """创建默认工具注册表（retrieve_knowledge + web_search）。

    Args:
        retriever: 内部法律知识库检索器
        tavily_client: Tavily 客户端（默认按 src.config 配置构建；未配置 Key 时
                       web_search 工具返回"搜索不可用"，不阻塞主流程）

    Returns:
        已注册两个内置工具的 ToolRegistry
    """
    registry = ToolRegistry()
    registry.register(RetrieveKnowledgeTool(retriever).build_spec())
    client = tavily_client or TavilySearchClient(
        api_key=TAVILY_API_KEY,
        timeout=TAVILY_TIMEOUT,
        max_results=TAVILY_MAX_RESULTS,
    )
    registry.register(WebSearchTool(client, default_max_results=TAVILY_MAX_RESULTS).build_spec())
    return registry
