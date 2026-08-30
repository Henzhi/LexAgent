"""
北大法宝 MCP 客户端（M3+ / F9 扩展，决策 D-PKULAW）。

将北大法宝（pkulaw.com）的 MCP 工具接入 LexAgent，作为**高权威官方法律源**
（法条原文 + 类案全文 + 核验 + 超链），优先级高于现有「国家法律法规数据库」
（仅目录、无正文）与「Tavily 域限定线索」。

设计要点（对照 AGENTS.md 与 pkulaw SKILL）：
- **懒加载 `mcp` SDK**：模块导入不依赖 mcp（本地/CI 未装也能 import）；只有
  实际构造客户端并调用时才 import，import 失败 → is_available()=False。
- **运行时按用途解析工具名**：北大法宝聚合服务（mcp-law-agg）把 10 个工具
  挂在**一个**端点下，名字带服务前缀且会变。故不硬编码工具名，而是
  `tools/list` 后按「用途关键词」匹配 name+description，建立 purpose→name 映射
  （见 _PURPOSE_KEYWORDS）；匹配失败兜底用 SKILL 记录的前缀名。
- **参数平铺**：北大法宝工具 inputSchema 常声明包装体但后端只认平铺（SKILL 3.1），
  故一律传平铺参数；调用失败按 SKILL 第四节阶梯降级（精简参数重试）。
- **结果按语义提取**：返回体形态不统一（裸数组 / 包裹体 Data / 纯字符串），按
  字段「语义」而非名字取值（SKILL 3.2），并清洗链接锚点 `.0` 后缀。
- **预算熔断（AGENTS.md 规则 8）**：每次成功调用前 check、后 record
  KIND_PKULAW；超限抛 BudgetExceededError（工具层据此降级，不阻断主链路）。
- **失败不抛异常中断上层**：公开方法失败抛 RuntimeError / BudgetExceededError，
  由工具层与 LegalSourceClient 门面归一化为 ok=False（与现有客户端一致）。

测试约束：本模块真实调用走网络 + mcp SDK，单测一律用 tests/fakes 的
FakePkulawClient（同接口、返回 canned 数据），不触达真实端点与 Key。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from src.config import PKULAW_ENABLED, PKULAW_MAX_RESULTS, PKULAW_MCP_TOKEN, PKULAW_MCP_URL, PKULAW_TIMEOUT

logger = logging.getLogger(__name__)


def _run_async(coro: Any) -> Any:
    """在「无运行循环则 asyncio.run；有运行循环则独立线程内 asyncio.run」执行协程。

    LangGraph 工具节点为同步函数，但图可能在 async 端点下被 astream 驱动，
    此时同步节点所在的 async 任务线程已有运行循环，直接 asyncio.run 会抛错；
    改用单线程执行新事件循环，避免污染外层循环。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # 当前线程无运行循环
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()

# 用途 → 匹配关键词（name 或 description 命中即认领该用途；顺序即优先级）
_PURPOSE_KEYWORDS: dict[str, list[str]] = {
    "article_search": ["search_article", "法条语义检索", "检索法条", "语义检索法条"],
    "article_exact": ["get_article", "精确取条", "取条"],
    "case_search": ["search_case", "类案语义检索", "案例语义检索"],
    "law_list": ["get_law_list", "法规列表", "law_list"],
    "verify_law": ["law_recognition", "法条识别", "法规名称识别", "法规识别"],
    "verify_case": ["case_number", "anhao", "案号识别", "案号验"],
    "verify_provision": ["adjust_provisions", "法条核验", "正文对照"],
    "add_links": ["add_doc_link", "get_linked_content", "超链", "加链接", "批量加"],
}

# 兜底工具名（SKILL L3 快照前缀；运行时优先用 tools/list 实际名）
_FALLBACK_TOOL_NAMES: dict[str, str] = {
    "article_search": "mcp-law-search_search_article",
    "article_exact": "mcp-law-search_get_article",
    "case_search": "mcp-case-search-service_search_case",
    "law_list": "law-pkulaw-mcp_get_law_list",
    "verify_law": "law-recognition_law_recognition",
    "verify_case": "case-number-recognition_anhao_recognition",
    "verify_provision": "chat-web_adjust_provisions",
    "add_links": "add-doc-link_get_linked_content",
}


class PkulawMCPClient:
    """北大法宝 MCP 客户端（聚合端点，单连接暴露全部工具）。

    用法（由 LegalSourceClient / ReAct 工具闭包注入）:
        client = PkulawMCPClient()          # 读 src.config
        if client.is_available():
            items = client.search_article("民法典 离婚冷静期")
    """

    def __init__(
        self,
        url: str = PKULAW_MCP_URL,
        token: str = PKULAW_MCP_TOKEN,
        timeout: float = PKULAW_TIMEOUT,
        enabled: bool = PKULAW_ENABLED,
    ):
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.enabled = bool(enabled)
        # purpose → 实际工具名（首次 list_tools 后填充，跨调用复用）
        self._tool_map: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 可用性（配置级 + SDK 可达）
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        if not self.enabled or not self.url or not self.token:
            return False
        try:
            import mcp  # noqa: F401  仅探测 SDK 是否安装

            return True
        except Exception as e:  # pragma: no cover - 依赖缺失路径
            logger.warning(f"北大法宝 MCP 不可用：mcp SDK 未安装（{e}）")
            return False

    # ------------------------------------------------------------------
    # 公开方法（同步；内部 async 经 asyncio.run 驱动）
    # ------------------------------------------------------------------
    def search_article(self, query: str, lib: str = "中央", max_results: int = PKULAW_MAX_RESULTS) -> list[dict]:
        """法条语义检索：自然语言 → 相关法条。建议带 lib='中央' 避免地方文件。"""
        params: dict[str, Any] = {"text": query, "lib": lib, "size": int(max_results or PKULAW_MAX_RESULTS)}
        raw = self._run("article_search", params)
        return self._normalize_search(raw, purpose="article")

    def get_article(self, title: str, number: str) -> list[dict]:
        """法条精确取条（首选）：明确《X法》第 X 条，number 中文/数字均可。"""
        params = {"title": title, "number": str(number)}
        raw = self._run("article_exact", params)
        return self._normalize_search(raw, purpose="article")

    def search_case(self, query: str, max_results: int = PKULAW_MAX_RESULTS) -> list[dict]:
        """类案语义检索：自然语言案情 → 类案（带回查明/认为/结果全文）。"""
        params = {"text": query, "size": int(max_results or PKULAW_MAX_RESULTS)}
        raw = self._run("case_search", params)
        return self._normalize_search(raw, purpose="case")

    def get_law_list(self, title: str, effectiveness: list[str] | None = None, max_results: int = PKULAW_MAX_RESULTS) -> list[dict]:
        """法规列表：关键词 → 法规元数据（合规清单/立法追踪）。"""
        params: dict[str, Any] = {"title": title}
        if effectiveness:
            params["effectiveness"] = list(effectiveness)
        params["size"] = int(max_results or PKULAW_MAX_RESULTS)
        raw = self._run("law_list", params)
        return self._normalize_search(raw, purpose="law")

    def verify_law(self, text: str) -> Any:
        """法条识别：抽取一段文本引用的法规名并标准化（不返回时效）。"""
        return self._run("verify_law", {"text": text})

    def verify_case(self, text: str) -> Any:
        """案号识别：验证一段文本中的案号是否能在库里查到。"""
        return self._run("verify_case", {"text": text})

    def verify_provision(self, userlaw: list[dict], answerlaw: list[dict], prompt: str = "") -> Any:
        """法条核验：将 AI 自写条文与权威原文对照（平铺参数，SKILL 3.1）。"""
        params: dict[str, Any] = {"userlaw": userlaw or [], "answerlaw": answerlaw or []}
        if prompt:
            params["prompt"] = prompt
        return self._run("verify_provision", params)

    def add_links(self, text: str) -> str:
        """超链：给整段文本批量加可点击的 pkulaw 原文链接（最后一步、仅 Markdown）。"""
        raw = self._run("add_links", {"message": text})
        # 超链工具返回纯字符串（SKILL 3.5）；若被 JSON 包裹则取文本
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for k in ("message", "text", "content", "result"):
                if isinstance(raw.get(k), str):
                    return raw[k]
        return str(raw)

    # ------------------------------------------------------------------
    # 预算埋点（AGENTS.md 规则 8：新增付费外部依赖必接预算）
    # ------------------------------------------------------------------
    def _run(self, purpose: str, arguments: dict[str, Any]) -> Any:
        """同步驱动一次 MCP 调用（含预算 check/record），失败抛 RuntimeError。

        兼容同步与异步调用上下文：若在运行中事件循环内（LangGraph 流式路径
        在 async 端点下执行本同步节点），asyncio.run 会报
        "cannot be called from a running event loop"，故回退到独立线程内运行。
        """
        from src.observability.cost_budget import BudgetExceededError, KIND_PKULAW, get_budget

        budget = get_budget()
        budget.check(KIND_PKULAW)  # 超限 → BudgetExceededError（上层识别为额度用尽）
        try:
            raw = _run_async(self._a_call(purpose, arguments))
        except BudgetExceededError:
            raise
        except Exception as e:
            raise RuntimeError(f"北大法宝 MCP 调用失败（{purpose}）: {e}") from e
        budget.record(KIND_PKULAW)
        return raw

    # ------------------------------------------------------------------
    # asyncio 实际调用
    # ------------------------------------------------------------------
    async def _a_call(self, purpose: str, arguments: dict[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.token}"}
        async with streamablehttp_client(self.url, headers=headers, timeout=self.timeout) as (
            read,
            write,
            _session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if not self._tool_map:
                    self._discover(session)
                tool_name = self._tool_for(purpose)
                result = await session.call_tool(tool_name, arguments)
                return self._parse_result(result)

    async def _discover(self, session: Any) -> None:
        """list_tools 后按用途关键词建立 purpose→工具名 映射。"""
        try:
            listed = await session.list_tools()
        except Exception as e:  # pragma: no cover - 网络相关
            logger.warning(f"北大法宝 tools/list 失败，回退静态工具名: {e}")
            self._tool_map = dict(_FALLBACK_TOOL_NAMES)
            return
        tools = getattr(listed, "tools", []) or []
        claimed: dict[str, str] = {}
        for tool in tools:
            name = (getattr(tool, "name", "") or "").lower()
            desc = (getattr(tool, "description", "") or "").lower()
            blob = f"{name} {desc}"
            for purpose, kws in _PURPOSE_KEYWORDS.items():
                if purpose in claimed:
                    continue
                if any(kw.lower() in blob for kw in kws):
                    claimed[purpose] = getattr(tool, "name", "")
        # 未匹配到的用途用兜底名兜底
        for purpose, fb in _FALLBACK_TOOL_NAMES.items():
            claimed.setdefault(purpose, fb)
        self._tool_map = claimed
        logger.info(f"北大法宝工具映射: {claimed}")

    def _tool_for(self, purpose: str) -> str:
        name = self._tool_map.get(purpose) or _FALLBACK_TOOL_NAMES.get(purpose)
        if not name:
            raise RuntimeError(f"北大法宝未找到用途'{purpose}'对应的工具")
        return name

    # ------------------------------------------------------------------
    # 结果解析与语义归一化
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_result(result: Any) -> Any:
        """提取 tool 结果文本 → JSON（若可）或原始字符串。"""
        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured
        blocks = getattr(result, "content", []) or []
        texts = [
            getattr(b, "text", "")
            for b in blocks
            if getattr(b, "type", None) == "text"
        ]
        raw = "\n".join(t for t in texts if t).strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return raw

    def _normalize_search(self, raw: Any, purpose: str = "article") -> list[dict]:
        """把 pkulaw 返回（裸数组 / 包裹体 Data / 字符串）归一化为条目列表。

        按语义规则取值（SKILL 3.2），不依赖固定字段名；链接清洗 `.0` 锚点后缀。
        """
        items: list[dict] = []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return items
        if isinstance(raw, dict):
            # 包裹体：取 Data / data（大写 Data 优先，见 SKILL 3.2）
            inner = raw.get("Data") if "Data" in raw else raw.get("data")
            if inner is not None:
                raw = inner
            elif "rows" in raw:
                raw = raw["rows"]
            elif "results" in raw:
                raw = raw["results"]
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []
        for it in raw:
            if not isinstance(it, dict):
                continue
            items.append(self._extract_item(it, purpose))
        return items

    @staticmethod
    def _strip_dotzero(url: str) -> str:
        """去掉法条链接锚点的 .0 坏后缀（SKILL 3.3，如 #tiao_1077.0 → #tiao_1077）。"""
        return re.sub(r"\.0(#|$)", r"\1", url) if url else url

    def _extract_item(self, it: dict, purpose: str) -> dict:
        """从单条结果按语义提取标准字段。"""
        # 标题
        title = (
            it.get("title") or it.get("lawName") or it.get("caseName")
            or it.get("name") or it.get("original") or ""
        )
        title = re.sub(r"<[^>]+>", "", str(title)).strip()
        # 链接：找含 pkulaw.com 的字段，若是 [名](url) 取裸链
        url = ""
        for k in ("url", "Url", "source", "link", "html", "docUrl"):
            v = it.get(k)
            if isinstance(v, str) and "pkulaw.com" in v:
                url = v
                m = re.search(r"\]\((https?://[^)]+)\)", v)
                if m:
                    url = m.group(1)
                break
        url = self._strip_dotzero(url)
        # 正文：法条用 article/FullText/original_text；案例拼接 查明/认为/结果
        content = self._extract_content(it)
        # 时效 / 效力位阶（可能是字符串或数组，见 SKILL 3.2）
        law_status = self._first_text(it.get("timeliness") or it.get("TimelinessDic") or it.get("law_status"))
        effectiveness = self._first_text(it.get("effectiveness") or it.get("EffectivenessDic"))
        case_number = it.get("CaseFlag") or it.get("caseFlag") or it.get("case_number") or ""
        court = it.get("Court") or it.get("courthouse_name") or it.get("court") or ""
        item = {
            "title": title,
            "url": url,
            "content": content,
            "law_status": law_status,
            "effectiveness": effectiveness,
        }
        if purpose == "case":
            item["case_number"] = str(case_number)
            item["court"] = str(court)
        return item

    @staticmethod
    def _first_text(v: Any) -> str:
        if isinstance(v, list):
            return str(v[0]) if v else ""
        return str(v) if v is not None else ""

    @staticmethod
    def _extract_content(it: dict) -> str:
        # 法条正文
        for k in ("article", "FullText", "original_text", "content", "text"):
            v = it.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # 案例判决要素链：查明 → 认为 → 结果
        parts = []
        for k in ("Ascertain", "Identified", "RefereeResult", "ascertain", "identified", "referee_result"):
            v = it.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
            elif isinstance(v, list):
                parts.append(" ".join(str(x) for x in v if x))
        return "\n".join(p for p in parts if p)
