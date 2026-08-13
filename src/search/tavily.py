"""
Tavily 网络搜索客户端封装（M1 / F3）。

- 对 tavily-python SDK（>=0.7.0）做轻量封装：超时、Key 缺失检测、异常归一化；
- `is_available()`：Key 缺失 / SDK 初始化失败时为 False（工具据此返回"搜索不可用"）；
- `search()`：统一返回结构化结果列表 [{title, url, content, score}]；
- 所有异常归一化为 RuntimeError，供工具层捕获后返回 ToolResult(ok=False)。

注意：tavily 包为运行时可选依赖（pyproject 已声明），此处延迟导入，
避免未安装时影响整个应用启动。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TavilySearchClient:
    """Tavily 官方 SDK 封装。

    用法:
        client = TavilySearchClient(api_key="tvly-xxx", timeout=15.0)
        if client.is_available():
            results = client.search("民事诉讼法 最新修订")
    """

    def __init__(self, api_key: str = "", timeout: float = 15.0, max_results: int = 5):
        """初始化。

        Args:
            api_key: Tavily API Key（为空时 is_available() 返回 False）
            timeout: 请求超时秒数
            max_results: 默认返回结果数
        """
        self.api_key = api_key or ""
        self.timeout = max(1.0, float(timeout))
        self.max_results = max(1, int(max_results))
        self._client: Any | None = None
        self._init_error: str = ""
        if self.api_key:
            self._init_client()

    def _init_client(self) -> None:
        """延迟初始化 TavilyClient（失败时记录错误，is_available 返回 False）。"""
        try:
            from tavily import TavilyClient
            try:
                self._client = TavilyClient(api_key=self.api_key, timeout=self.timeout)
            except TypeError:
                # 兼容旧版 SDK 无 timeout 参数
                self._client = TavilyClient(api_key=self.api_key)
        except Exception as e:
            self._init_error = str(e)
            logger.warning(f"TavilyClient 初始化失败: {e}")

    def is_available(self) -> bool:
        """是否可用（已配置 Key 且 SDK 初始化成功）。"""
        return self._client is not None

    def search(self, query: str, max_results: int | None = None) -> list[dict]:
        """执行网络搜索。

        Args:
            query: 搜索关键词
            max_results: 返回结果数（默认使用构造时的 max_results，上限 10）

        Returns:
            结构化结果列表 [{title, url, content, score}]

        Raises:
            RuntimeError: 未配置 / 初始化失败 / 搜索异常（由工具层捕获归一化）
        """
        if not self.is_available():
            detail = self._init_error or "缺少 TAVILY_API_KEY"
            raise RuntimeError(f"Tavily 未配置或初始化失败: {detail}")
        k = max(1, min(max_results or self.max_results, 10))
        try:
            resp = self._client.search(query=query, max_results=k)
        except Exception as e:
            raise RuntimeError(f"Tavily 搜索失败: {e}") from e

        raw_results = (resp or {}).get("results", []) or []
        results = []
        for r in raw_results:
            if not isinstance(r, dict):
                continue
            results.append({
                "title": (r.get("title") or "").strip(),
                "url": (r.get("url") or "").strip(),
                "content": (r.get("content") or "").strip(),
                "score": float(r.get("score") or 0.0),
            })
        return results
