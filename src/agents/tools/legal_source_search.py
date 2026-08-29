"""
legal_source_search 内置工具（M2 / F9）。

封装 `LegalSourceClient.search(query, source_type)` 为 Agent 可调用工具：
- 国家法律法规数据库（法规验证）+ 人民法院案例库（官方域线索）+ 小包公（可选）；
- 用于网络线索的**回源二次验证**（REQ-E4）与权威法规检索，source=legal_source；
- 官方源不可达 → ToolResult(ok=False)，summary 首词"权威源检索失败"，
  不静默回退 Tavily（D-M2-4：让 LLM 知道验证不可用，REQ-UW1 语义）。
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from src.agents.tools.base import (
    CATEGORY_LEGAL,
    SOURCE_LEGAL,
    ToolResult,
    ToolSpec,
    tool,
    truncate_summary,
)
from src.search.legal_sources import LegalSourceClient

logger = logging.getLogger(__name__)

# 单次工具结果摘要最多展示的条数（summary ≤300 字符约束）
_SUMMARY_MAX_ITEMS = 3
# 单条条目内容截断长度
_SUMMARY_ITEM_CHARS = 40


def _build_summary(data: dict[str, Any]) -> str:
    """构建摘要：命中数 + 来源 + 前几条（title — 状态/URL）。"""
    results = data.get("results") or []
    if not results:
        return "官方源未检索到匹配结果"
    sources = data.get("sources") or []
    src_label = "、".join(sources) if sources else "官方源"
    parts = [f"检索到 {len(results)} 条官方源结果（来源: {src_label}）："]
    for i, r in enumerate(results[:_SUMMARY_MAX_ITEMS], start=1):
        title = (r.get("title") or "").strip() or "无标题"
        status = (r.get("law_status") or "").strip()
        extra = f"（{status}）" if status else ""
        parts.append(f"{i}) {title}{extra}")
    return truncate_summary("；".join(parts))


def build_legal_source_search_spec(client: LegalSourceClient) -> ToolSpec:
    """构造 legal_source_search 工具的 ToolSpec（依赖经闭包注入）。

    Args:
        client: 官方法律源统一门面客户端
    """

    @tool(name="legal_source_search", category=CATEGORY_LEGAL)
    def legal_source_search(
        query: Annotated[str, "检索关键词，建议使用规范法律名称，如 '民事诉讼法'"],
        source_type: Annotated[
            Literal["law", "case", "all"],
            "检索源类型：law=仅法规（默认），case=仅案例，all=两者",
        ] = "law",
    ) -> ToolResult:
        """检索官方法律权威源：国家法律法规数据库与人民法院案例库。

        国家法律法规数据库可验证法规现行有效性、最新版本与修订状态；
        人民法院案例库提供权威案例。当需要验证网络搜索到的法规线索、
        确认法条是否现行有效、或查找权威案例时调用本工具；
        结果为官方源，可信度高于网络搜索。
        """
        st = source_type if source_type in ("law", "case", "all") else "law"
        if not client.is_available():
            return ToolResult(
                tool="legal_source_search",
                call_id="",
                ok=False,
                summary="权威源检索失败：官方法律源均未配置",
                data={},
                source=SOURCE_LEGAL,
            )
        try:
            data = client.search(query, source_type=st)
            return ToolResult(
                tool="legal_source_search",
                call_id="",
                ok=True,
                summary=_build_summary(data),
                data={"results": data["results"], "count": data["count"],
                      "sources": data["sources"]},
                source=SOURCE_LEGAL,
            )
        except Exception as e:
            logger.warning(f"legal_source_search 检索失败（权威源检索失败）: {e}")
            return ToolResult(
                tool="legal_source_search",
                call_id="",
                ok=False,
                summary=f"权威源检索失败: {e}",
                data={},
                source=SOURCE_LEGAL,
            )

    return legal_source_search
