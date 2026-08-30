"""
搜索包导出（M1 / F3）。

TavilySearchClient：Tavily 官方 SDK 轻量封装（超时 / Key 校验 / 异常归一化 / is_available）。
工具层只依赖本封装，便于将来替换搜索供应商。
"""

from __future__ import annotations

from src.search.tavily import TavilySearchClient

__all__ = ["TavilySearchClient"]
