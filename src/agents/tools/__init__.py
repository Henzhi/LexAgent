"""
工具包导出（M1 / F1 + M2 / F9）。

`build_default_tools(retriever)` 是便捷工厂：注册 retrieve_knowledge + web_search
两个内置工具；LEGAL_SOURCE_ENABLED=true（默认）时追加 legal_source_search；
PKULAW_ENABLED=true 且客户端可用时追加 pkulaw_search / pkulaw_verify。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from src.agents.tools.base import (
    CATEGORY_KNOWLEDGE,
    CATEGORY_LEGAL,
    CATEGORY_WEB,
    SOURCE_INTERNAL_KB,
    SOURCE_LEGAL,
    SOURCE_WEB,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    tool,
    truncate_summary,
)
from src.agents.tools.registry import ToolRegistry
from src.agents.tools.retrieve_knowledge import build_retrieve_knowledge_spec
from src.agents.tools.web_search import build_web_search_spec
from src.agents.tools.legal_source_search import build_legal_source_search_spec
from src.agents.tools.pkulaw_search import build_pkulaw_search_spec, build_pkulaw_verify_spec
from src.config import (
    LEGAL_SOURCE_ENABLED,
    PKULAW_ENABLED,
    TAVILY_API_KEY,
    TAVILY_MAX_RESULTS,
    TAVILY_TIMEOUT,
)
from src.rag.retriever import BaseRetriever
from src.search.legal_sources import CourtCaseLibraryClient, LegalSourceClient
from src.search.pkulaw_mcp import PkulawMCPClient
from src.search.tavily import TavilySearchClient

__all__ = [
    "ToolSpec",
    "ToolResult",
    "ToolExecutionError",
    "ToolRegistry",
    "tool",
    "build_retrieve_knowledge_spec",
    "build_web_search_spec",
    "build_legal_source_search_spec",
    "build_pkulaw_search_spec",
    "build_pkulaw_verify_spec",
    "build_default_tools",
    "CATEGORY_KNOWLEDGE",
    "CATEGORY_WEB",
    "CATEGORY_LEGAL",
    "SOURCE_INTERNAL_KB",
    "SOURCE_WEB",
    "SOURCE_LEGAL",
    "truncate_summary",
]


def build_default_tools(
    retriever: BaseRetriever,
    tavily_client: TavilySearchClient | None = None,
    legal_client: LegalSourceClient | None = None,
    pkulaw_client: PkulawMCPClient | None = None,
) -> ToolRegistry:
    """创建默认工具注册表。

    Args:
        retriever: 内部法律知识库检索器
        tavily_client: Tavily 客户端（默认按 src.config 配置构建；未配置 Key 时
                       web_search 工具返回"搜索不可用"，不阻塞主流程）
        legal_client: 官方法律源客户端（默认按配置构建；LEGAL_SOURCE_ENABLED=false
                      时不注册 legal_source_search 工具）
        pkulaw_client: 北大法宝 MCP 客户端（默认按配置构建；PKULAW_ENABLED=false 或
                       未配置 URL/Token 时不注册 pkulaw_search / pkulaw_verify 工具）

    Returns:
        已注册内置工具的 ToolRegistry
    """
    registry = ToolRegistry()
    registry.register(build_retrieve_knowledge_spec(retriever))
    client = tavily_client or TavilySearchClient(
        api_key=TAVILY_API_KEY,
        timeout=TAVILY_TIMEOUT,
        max_results=TAVILY_MAX_RESULTS,
    )
    registry.register(build_web_search_spec(client, default_max_results=TAVILY_MAX_RESULTS))
    if LEGAL_SOURCE_ENABLED:
        # 官方案例库线索依赖 Tavily 域限定搜索，复用同一 client
        legal = legal_client or LegalSourceClient(
            court_case=CourtCaseLibraryClient(tavily_client=client),
        )
        registry.register(build_legal_source_search_spec(legal))
    if PKULAW_ENABLED:
        # 北大法宝：高权威官方法律源（检索 + 反幻觉核验），按可用性注册
        pk = pkulaw_client or PkulawMCPClient()
        if pk.is_available():
            registry.register(build_pkulaw_search_spec(pk))
            registry.register(build_pkulaw_verify_spec(pk))
        else:
            logger.warning("北大法宝 MCP 未配置或未安装 SDK，跳过 pkulaw 工具注册")
    return registry
