"""
web_search 内置工具（M1 / F3）。

封装 TavilySearchClient 为 Agent 可调用工具：
- schema 含 query / max_results；
- 网络结果仅作线索，不得直接作为最终法律依据（REQ-U3 / 决策 5），source=web；
- Tavily 失败/超时/未配置 → ToolResult(ok=False)，summary 首词"搜索不可用"（REQ-UW1），
  不中断 ReAct 循环，LLM 据此仅基于内部库回答。
"""
from __future__ import annotations

import logging
from typing import Any

from src.agents.tools.base import (
    CATEGORY_WEB,
    SOURCE_WEB,
    ToolResult,
    ToolSpec,
    truncate_summary,
)
from src.search.tavily import TavilySearchClient

logger = logging.getLogger(__name__)

# 单次工具结果摘要最多展示的网页数
_SUMMARY_MAX_RESULTS = 3


def _build_summary(results: list[dict]) -> str:
    """构建摘要：命中数 + 前几条（title — url）。"""
    if not results:
        return "未搜索到相关网络结果"
    parts = [f"搜索到 {len(results)} 条网络结果（仅作线索，需结合内部库判断）："]
    for i, r in enumerate(results[:_SUMMARY_MAX_RESULTS], start=1):
        title = (r.get("title") or "").strip() or "无标题"
        url = (r.get("url") or "").strip()
        parts.append(f"{i}) {title}" + (f"（{url}）" if url else ""))
    return truncate_summary("；".join(parts))


class WebSearchTool:
    """Tavily 通用网络搜索工具。"""

    def __init__(self, client: TavilySearchClient, default_max_results: int = 5):
        """初始化。

        Args:
            client: Tavily 搜索客户端封装
            default_max_results: 默认返回结果数
        """
        self.client = client
        self.default_max_results = default_max_results

    def build_spec(self) -> ToolSpec:
        """构造工具自描述（OpenAI 兼容 schema）。"""
        return ToolSpec(
            name="web_search",
            description=(
                "搜索互联网获取最新法律法规、司法解释、案例等线索。"
                "当涉及最新修订、时效性信息、外部案例时使用；"
                "网络搜索结果仅作线索，不得直接作为最终法律依据。"
            ),
            parameters={
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 '民事诉讼法 最新修订 2026'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果数，默认 5，最大 10",
                },
            },
            required=["query"],
            category=CATEGORY_WEB,
            executor=self._exec,
        )

    def _exec(self, query: str, max_results: int | None = None) -> ToolResult:
        """执行网络搜索（失败归一化为 ok=False 的"搜索不可用"，不抛出）。"""
        if not self.client.is_available():
            return ToolResult(
                tool="web_search",
                call_id="",
                ok=False,
                summary="搜索不可用：未配置 TAVILY_API_KEY 或初始化失败",
                data={},
                source=SOURCE_WEB,
            )
        try:
            k = int(max_results) if max_results else self.default_max_results
            k = max(1, min(k, 10))
            results = self.client.search(query, max_results=k)
            return ToolResult(
                tool="web_search",
                call_id="",
                ok=True,
                summary=_build_summary(results),
                data={"results": results, "count": len(results)},
                source=SOURCE_WEB,
            )
        except Exception as e:
            logger.warning(f"web_search 搜索失败（搜索不可用）: {e}")
            return ToolResult(
                tool="web_search",
                call_id="",
                ok=False,
                summary=f"搜索不可用: {e}",
                data={},
                source=SOURCE_WEB,
            )
