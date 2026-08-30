"""
官方法律源客户端（M2 / F9，决策 D-R4 / D-M2-3 / D-M2-4）。

三路来源，统一返回结构 [{title, url, content, source, extra}]：
- NationalLawClient：国家法律法规数据库（flk.npc.gov.cn）公开 JSON 检索接口；
- CourtCaseLibraryClient：人民法院案例库（anli.court.gov.cn）无公开 API，
  用 Tavily 域限定搜索发现案例线索（include_domains），标注官方域线索；
- XiaobaogongClient：小包公第三方案例 API（可选，XBG_API_KEY + XBG_API_URL
  都配置才启用）。

设计约束：
- 所有异常归一化为 RuntimeError，由工具层捕获后返回 ToolResult(ok=False)；
- `is_available()` 仅做配置级检查（不发网络请求）；
- 官方接口失败不静默回退 Tavily（D-M2-4）：验证不可用要让 LLM 知道。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from src.config import (
    LEGAL_SOURCE_MAX_RESULTS,
    LEGAL_SOURCE_TIMEOUT,
    XBG_API_KEY,
    XBG_API_URL,
)
from src.observability.cost_budget import BudgetExceededError
from src.search.pkulaw_mcp import PkulawMCPClient

logger = logging.getLogger(__name__)

# 来源标识（ToolResult.data 与 fusion 使用的 source 粒度）
SOURCE_NATIONAL_LAW_DB = "national_law_db"  # 国家法律法规数据库
SOURCE_COURT_CASE_LIB = "court_case_lib"  # 人民法院案例库（官方域线索）
SOURCE_XBG = "xiaobaogong"  # 小包公（第三方）
SOURCE_PKULAW = "pkulaw"  # 北大法宝 MCP（高权威官方法律源）

# 法规状态码（flk.npc.gov.cn 返回的 sxx 字段）→ 中文说明
# 来源：前端 JS L4e=[{label:"尚未生效",key:4},{label:"有效",key:3},{label:"已修改",key:2},{label:"已废止",key:1}]
_LAW_STATUS_MAP = {
    1: "已废止",
    2: "已修改",
    3: "现行有效",
    4: "尚未生效",
}


def _norm_item(
    title: str,
    url: str,
    content: str,
    source: str,
    **extra: Any,
) -> dict[str, Any]:
    """统一结果条目结构。"""
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "content": (content or "").strip(),
        "source": source,
        **extra,
    }


class NationalLawClient:
    """国家法律法规数据库（flk.npc.gov.cn）检索客户端。

    用法:
        client = NationalLawClient(timeout=10.0)
        results = client.search_law("民事诉讼法")
    """

    SEARCH_URL = "https://flk.npc.gov.cn/law-search/search/list"
    # 详情页前缀：接口不返回 url，用 bbbs 构造详情链接
    DETAIL_BASE = "https://flk.npc.gov.cn/detail2.html"

    def __init__(self, timeout: float = LEGAL_SOURCE_TIMEOUT):
        self.timeout = max(1.0, float(timeout))

    def is_available(self) -> bool:
        """配置级可用（无需 Key，始终可用；实际连通性由 search 抛错体现）。"""
        return True

    def search_law(self, keyword: str, max_results: int = LEGAL_SOURCE_MAX_RESULTS) -> list[dict]:
        """按关键词检索法律法规目录。

        Raises:
            RuntimeError: 接口不可达 / 返回非 JSON / 结构异常（由工具层归一化）
        """
        k = max(1, min(int(max_results or 5), 10))
        # 新版 API（2025 改版）使用 JSON body，字段名与旧版不同
        payload_body = {
            "searchContent": keyword or "",
            "searchType": 2,  # 2=模糊匹配
            "searchRange": 1,  # 1=法规
            "page": 1,
            "size": k,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LexAgent/0.7",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = requests.post(
                self.SEARCH_URL,
                json=payload_body,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            raise RuntimeError(f"国家法律法规数据库接口请求失败: {e}") from e

        try:
            records = (payload or {}).get("rows") or []
        except Exception as e:
            raise RuntimeError(f"国家法律法规数据库返回结构异常: {e}") from e

        results: list[dict] = []
        for r in records:
            if not isinstance(r, dict):
                continue
            bbbs = (r.get("bbbs") or "").strip()
            url = f"{self.DETAIL_BASE}?bbbs={bbbs}" if bbbs else ""
            # title 含 <em> 高亮标签，清掉
            raw_title = r.get("title") or ""
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            sxx = r.get("sxx")
            results.append(
                _norm_item(
                    title=title,
                    url=url,
                    content="",  # 列表接口不返回正文摘要
                    source=SOURCE_NATIONAL_LAW_DB,
                    office=r.get("zdjgName") or "",  # 发布机关
                    publish_date=r.get("gbrq") or "",  # 公布日期
                    effective_date=r.get("sxrq") or "",  # 生效日期
                    law_status=_LAW_STATUS_MAP.get(sxx, str(sxx) if sxx is not None else "未知"),
                    law_type=r.get("flxz") or "",  # 法律性质（法律/行政法规等）
                )
            )
        return results


class CourtCaseLibraryClient:
    """人民法院案例库（anli.court.gov.cn）案例线索发现。

    官方案例库无公开 API（PRD 待确认事项 Q4），采用 Tavily 域限定搜索
    （include_domains=["anli.court.gov.cn"]）发现官方域内案例线索（D-M2-3）。
    未配置 Tavily Key 时 is_available() 为 False。
    """

    DOMAIN = "anli.court.gov.cn"

    def __init__(self, tavily_client=None):
        """Args: tavily_client: TavilySearchClient（域限定搜索底层）"""
        self._tavily = tavily_client

    def is_available(self) -> bool:
        return bool(self._tavily and self._tavily.is_available())

    def search_case(self, keyword: str, max_results: int = LEGAL_SOURCE_MAX_RESULTS) -> list[dict]:
        """域限定检索官方案例库线索。

        Raises:
            RuntimeError: Tavily 未配置或搜索失败
        """
        if not self.is_available():
            raise RuntimeError("人民法院案例库线索检索不可用：未配置 TAVILY_API_KEY")
        query = f"{keyword} site:{self.DOMAIN}" if keyword else self.DOMAIN
        try:
            raw = self._tavily.search(query, max_results=max_results)
        except Exception as e:
            raise RuntimeError(f"人民法院案例库线索检索失败: {e}") from e
        # 只保留官方域内的结果，滤掉搜索引擎混入的其他域名
        return [
            _norm_item(
                title=r.get("title") or "",
                url=r.get("url") or "",
                content=r.get("content") or "",
                source=SOURCE_COURT_CASE_LIB,
                score=float(r.get("score") or 0.0),
            )
            for r in raw
            if self.DOMAIN in (r.get("url") or "")
        ]


class XiaobaogongClient:
    """小包公（第三方案例检索 API，可选，REQ-O2）。

    接口规格未定（PRD 待确认事项 Q2），按"POST JSON {query, top_k} +
    Bearer Key"的通用约定实现，实际采购后按官方文档调整解析逻辑。
    XBG_API_KEY 与 XBG_API_URL 都配置时才可用。
    """

    def __init__(self, api_key: str = XBG_API_KEY, api_url: str = XBG_API_URL, timeout: float = LEGAL_SOURCE_TIMEOUT):
        self.api_key = (api_key or "").strip()
        self.api_url = (api_url or "").strip()
        self.timeout = max(1.0, float(timeout))

    def is_available(self) -> bool:
        return bool(self.api_key and self.api_url)

    def search_case(self, keyword: str, max_results: int = LEGAL_SOURCE_MAX_RESULTS) -> list[dict]:
        """第三方案例检索（失败抛 RuntimeError，由工具层归一化）。"""
        if not self.is_available():
            raise RuntimeError("小包公 API 未配置")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"query": keyword or "", "top_k": max(1, int(max_results or 5))}
        try:
            resp = requests.post(self.api_url, json=body, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            raise RuntimeError(f"小包公 API 请求失败: {e}") from e
        # 宽松解析：兼容 {"results": [...]} / {"data": [...]} / 顶层列表
        rows = payload.get("results") or payload.get("data") or payload.get("cases") or []
        if isinstance(payload, list):
            rows = payload
        return [
            _norm_item(
                title=r.get("title") or r.get("caseName") or "",
                url=r.get("url") or "",
                content=r.get("content") or r.get("summary") or "",
                source=SOURCE_XBG,
            )
            for r in rows
            if isinstance(r, dict)
        ]


class PkulawLegalClient:
    """北大法宝 MCP 适配为 LegalSourceClient 子源（M3+ / F9 扩展）。

    将 PkulawMCPClient 的语义检索结果归一化为 legal_sources 统一条目结构
    （_norm_item），source 固定为 SOURCE_PKULAW。失败抛 RuntimeError，
    由门面 LegalSourceClient 归入 errors，不阻断其他子源。
    """

    def __init__(self, client: PkulawMCPClient | None = None):
        self._client = client or PkulawMCPClient()

    def is_available(self) -> bool:
        return self._client.is_available()

    def search_law(self, keyword: str, max_results: int = LEGAL_SOURCE_MAX_RESULTS) -> list[dict]:
        """法条语义检索（带 lib='中央'，避免地方文件挤占）。"""
        if not self.is_available():
            raise RuntimeError("北大法宝 MCP 未配置（URL/Token 缺失或 SDK 未安装）")
        try:
            items = self._client.search_article(keyword, lib="中央", max_results=max_results)
        except BudgetExceededError:
            # 额度用尽原样上抛（门面按「法宝额度已用尽」语义归入 errors），
            # 不与普通故障混同包装成 RuntimeError（丢失熔断语义）
            raise
        except Exception as e:
            raise RuntimeError(f"北大法宝法条检索失败: {e}") from e
        return [
            _norm_item(
                title=it.get("title") or "",
                url=it.get("url") or "",
                content=it.get("content") or "",
                source=SOURCE_PKULAW,
                law_status=it.get("law_status") or "",
                effectiveness=it.get("effectiveness") or "",
            )
            for it in items
        ]

    def search_case(self, keyword: str, max_results: int = LEGAL_SOURCE_MAX_RESULTS) -> list[dict]:
        """类案语义检索（带回查明/认为/结果全文）。"""
        if not self.is_available():
            raise RuntimeError("北大法宝 MCP 未配置（URL/Token 缺失或 SDK 未安装）")
        try:
            items = self._client.search_case(keyword, max_results=max_results)
        except BudgetExceededError:
            raise
        except Exception as e:
            raise RuntimeError(f"北大法宝类案检索失败: {e}") from e
        return [
            _norm_item(
                title=it.get("title") or "",
                url=it.get("url") or "",
                content=it.get("content") or "",
                source=SOURCE_PKULAW,
                case_number=it.get("case_number") or "",
                court=it.get("court") or "",
            )
            for it in items
        ]


class LegalSourceClient:
    """官方法律源统一门面（legal_source_search 工具的执行后端）。

    用法:
        client = LegalSourceClient()
        if client.is_available():
            data = client.search("民事诉讼法", source_type="law")
            # data == {"results": [...], "count": n, "sources": ["national_law_db"]}
    """

    def __init__(
        self,
        national_law: NationalLawClient | None = None,
        court_case: CourtCaseLibraryClient | None = None,
        xbg: XiaobaogongClient | None = None,
        pkulaw: PkulawLegalClient | None = None,
    ):
        self.national_law = national_law or NationalLawClient()
        self.court_case = court_case or CourtCaseLibraryClient()
        self.xbg = xbg or XiaobaogongClient()
        self.pkulaw = pkulaw or PkulawLegalClient()

    def is_available(self) -> bool:
        """任一子源配置级可用即视为可用。"""
        return (
            self.national_law.is_available()
            or self.court_case.is_available()
            or self.xbg.is_available()
            or self.pkulaw.is_available()
        )

    def search(
        self,
        query: str,
        source_type: str = "all",
        max_results: int = LEGAL_SOURCE_MAX_RESULTS,
    ) -> dict[str, Any]:
        """聚合检索官方法律源。

        Args:
            query: 检索关键词（法名 / 案例关键词）
            source_type: law=仅法规 | case=仅案例 | all=两者
            max_results: 每个子源返回条数上限

        Returns:
            {"results": [统一条目], "count": 总数, "sources": [实际命中的来源],
             "errors": [子源失败信息]}——子源失败不阻断其他子源（归入 errors）

        Raises:
            RuntimeError: query 为空或全部子源同时失败
        """
        if not (query or "").strip():
            raise RuntimeError("检索关键词为空")
        if source_type not in ("law", "case", "all"):
            source_type = "all"

        results: list[dict] = []
        errors: list[str] = []
        tried = 0

        if source_type in ("law", "all"):
            tried += 1
            try:
                results.extend(self.national_law.search_law(query, max_results))
            except RuntimeError as e:
                logger.warning(f"国家法律法规数据库检索失败: {e}")
                errors.append(str(e))
            # 北大法宝：高权威法条原文（D-PKULAW），与法规目录互补
            if self.pkulaw.is_available():
                tried += 1
                try:
                    results.extend(self.pkulaw.search_law(query, max_results))
                except RuntimeError as e:
                    logger.warning(f"北大法宝法条检索失败: {e}")
                    errors.append(str(e))

        if source_type in ("case", "all"):
            # 官方案例库线索（Tavily 域限定）优先，小包公与北大法宝补充
            if self.court_case.is_available():
                tried += 1
                try:
                    results.extend(self.court_case.search_case(query, max_results))
                except RuntimeError as e:
                    logger.warning(f"人民法院案例库线索检索失败: {e}")
                    errors.append(str(e))
            if self.xbg.is_available():
                tried += 1
                try:
                    results.extend(self.xbg.search_case(query, max_results))
                except RuntimeError as e:
                    logger.warning(f"小包公案例检索失败: {e}")
                    errors.append(str(e))
            if self.pkulaw.is_available():
                tried += 1
                try:
                    results.extend(self.pkulaw.search_case(query, max_results))
                except RuntimeError as e:
                    logger.warning(f"北大法宝类案检索失败: {e}")
                    errors.append(str(e))

        if tried == 0:
            raise RuntimeError("官方法律源均未配置（法规库不可达且未配置案例源）")
        if not results and errors and tried == len(errors):
            raise RuntimeError("；".join(errors))

        # 按来源去重（同一 url 只保留一条）
        seen: set[str] = set()
        deduped: list[dict] = []
        for r in results:
            key = r.get("url") or r.get("title") or ""
            if key and key in seen:
                continue
            seen.add(key)
            deduped.append(r)

        return {
            "results": deduped,
            "count": len(deduped),
            "sources": sorted({r["source"] for r in deduped}),
            "errors": errors,
        }
