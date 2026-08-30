"""
M3+ 北大法宝 MCP 接入测试（F9 扩展，决策 D-PKULAW）。

全部使用 FakePkulawClient / MagicMock，不触达真实 pkulaw 端点与 Bearer Token
（AGENTS.md 禁止事项）。覆盖：客户端归一化、官方法律源门面集成、融合验证状态、
ReAct 工具封装、预算熔断降级、build_default_tools 注册。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.tools import build_default_tools
from src.agents.tools.base import SOURCE_LEGAL
from src.agents.tools.pkulaw_search import build_pkulaw_search_spec, build_pkulaw_verify_spec
from src.observability.cost_budget import BudgetExceededError
from src.search.fusion import VERIFIED_OFFICIAL, fuse_evidence
from src.search.legal_sources import (
    SOURCE_PKULAW,
    LegalSourceClient,
    PkulawLegalClient,
)
from src.search.pkulaw_mcp import PkulawMCPClient
from tests.fakes import FakePkulawClient, FakeRetriever


# ---------------------------------------------------------------------------
# PkulawMCPClient 归一化
# ---------------------------------------------------------------------------

class TestPkulawNormalization:
    def test_strip_dotzero(self):
        assert PkulawMCPClient._strip_dotzero("https://x#tiao_1077.0") == "https://x#tiao_1077"
        assert PkulawMCPClient._strip_dotzero("https://x#tiao_1077") == "https://x#tiao_1077"

    def test_normalize_wrapped_data(self):
        """包裹体（大写 Data）→ 条目列表，链接 .0 锚点被清理。"""
        raw = {"Message": "成功", "Data": [{"title": "民法典", "url": "https://pkulaw.com/1#tiao_1077.0", "article": "第一条", "timeliness": "现行有效"}], "Total": 1}
        items = PkulawMCPClient()._normalize_search(raw, purpose="article")
        assert len(items) == 1
        it = items[0]
        assert it["title"] == "民法典"
        assert it["url"] == "https://pkulaw.com/1#tiao_1077"
        assert "第一条" in it["content"]
        assert it["law_status"] == "现行有效"

    def test_normalize_markdown_link_field(self):
        """url 字段为 [名](url) 形态 → 提取裸链。"""
        raw = [{"title": "X法", "url": "[《X法》第1条](https://pkulaw.com/x#tiao_1)", "article": "内容"}]
        items = PkulawMCPClient()._normalize_search(raw, purpose="article")
        assert items[0]["url"] == "https://pkulaw.com/x#tiao_1"

    def test_normalize_case_extracts_fields(self):
        raw = [{"title": "某案", "url": "https://pkulaw.com/c/1", "Ascertain": "查明事实", "Identified": "认为", "RefereeResult": "结果", "CaseFlag": "(2024)京01民终1号", "Court": "北京一中院"}]
        items = PkulawMCPClient()._normalize_search(raw, purpose="case")
        it = items[0]
        assert it["case_number"] == "(2024)京01民终1号"
        assert it["court"] == "北京一中院"
        assert "查明事实" in it["content"] and "结果" in it["content"]


# ---------------------------------------------------------------------------
# 运行时工具名解析（历史 Bug：_discover 漏 await）
# ---------------------------------------------------------------------------

class TestPkulawToolDiscovery:
    """守护「运行时按用途解析工具名」不退化为静态兜底名。

    历史 Bug：`_a_call` 里调用 `async def _discover` 漏了 `await`，协程从不执行，
    `_tool_map` 永远为空 → 静默退化到 `_FALLBACK_TOOL_NAMES`。而真实端点的工具名
    用 `.` 分隔（`mcp-law-search-service.search_article`），兜底快照用 `_` 分隔，
    退化后真实调用必然全部失败，且不抛异常、极难发现。
    """

    def test_discover_maps_real_tool_names(self):
        """list_tools 返回的真实名（点分隔）必须覆盖兜底名（下划线分隔）。"""
        import asyncio

        session = MagicMock()
        listed = MagicMock()
        listed.tools = [
            MagicMock(name_="x", description=""),
        ]
        # MagicMock 的 name 需显式赋值（构造参数 name 有特殊含义）
        tools = []
        for real_name in [
            "mcp-law-search-service.search_article",
            "mcp-law-search-service.get_article",
            "mcp-case-search-service.search_case",
            "mcp-law.get_law_list",
            "law_recognition.law_recognition",
            "case_number_recognition.anhao_recognition",
            "pku_citation_validator.adjust_provisions",
            "add-doc-link.get_linked_content",
        ]:
            t = MagicMock()
            t.name = real_name
            t.description = ""
            tools.append(t)
        listed.tools = tools

        async def _list_tools():
            return listed

        session.list_tools = _list_tools
        client = PkulawMCPClient()
        asyncio.run(client._discover(session))

        assert client._tool_map, "_tool_map 为空说明 _discover 未生效（漏 await）"
        # 8 个用途全部解析到点分隔真实名，无一退化为下划线兜底名
        assert client._tool_map["article_search"] == "mcp-law-search-service.search_article"
        assert client._tool_map["article_exact"] == "mcp-law-search-service.get_article"
        assert client._tool_map["case_search"] == "mcp-case-search-service.search_case"
        assert client._tool_map["law_list"] == "mcp-law.get_law_list"
        assert client._tool_map["verify_law"] == "law_recognition.law_recognition"
        assert client._tool_map["verify_case"] == "case_number_recognition.anhao_recognition"
        assert client._tool_map["verify_provision"] == "pku_citation_validator.adjust_provisions"
        assert client._tool_map["add_links"] == "add-doc-link.get_linked_content"

    def test_discover_is_awaited_in_call_path(self):
        """_a_call 必须 await _discover：调用时用运行时名而非兜底名。"""
        import asyncio
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        called: dict = {}

        session = MagicMock()

        async def _initialize():
            return None

        async def _list_tools():
            listed = MagicMock()
            t = MagicMock()
            t.name = "mcp-law-search-service.search_article"
            t.description = ""
            listed.tools = [t]
            return listed

        async def _call_tool(name, arguments):
            called["name"] = name
            result = MagicMock()
            result.content = []
            result.structuredContent = {"Data": []}
            result.isError = False
            return result

        session.initialize = _initialize
        session.list_tools = _list_tools
        session.call_tool = _call_tool

        @asynccontextmanager
        async def fake_http(url, headers=None, timeout=None):
            yield (MagicMock(), MagicMock(), None)

        @asynccontextmanager
        async def fake_session(read, write):
            yield session

        client = PkulawMCPClient()
        with patch("mcp.client.streamable_http.streamablehttp_client", fake_http), patch(
            "mcp.ClientSession", fake_session
        ):
            asyncio.run(client._a_call("article_search", {"text": "x"}))

        assert called["name"] == "mcp-law-search-service.search_article", (
            f"实际调用了 {called.get('name')!r}，说明 _discover 未 await 而退化为兜底名"
        )


# ---------------------------------------------------------------------------
# PkulawLegalClient（门面子源适配）
# ---------------------------------------------------------------------------

class TestPkulawLegalClient:
    def test_search_law_maps_to_norm_item(self):
        client = PkulawLegalClient(FakePkulawClient())
        results = client.search_law("民法典")
        assert len(results) == 1
        r = results[0]
        assert r["source"] == SOURCE_PKULAW
        assert r["title"] == "中华人民共和国民法典"
        assert r["law_status"] == "现行有效"

    def test_search_case_maps_to_norm_item(self):
        client = PkulawLegalClient(FakePkulawClient())
        results = client.search_case("买卖合同")
        assert results[0]["source"] == SOURCE_PKULAW
        assert results[0]["case_number"] == "(2024)京01民终123号"

    def test_unavailable_raises_runtime(self):
        client = PkulawLegalClient(FakePkulawClient(available=False))
        assert client.is_available() is False
        with pytest.raises(RuntimeError):
            client.search_law("测试")


# ---------------------------------------------------------------------------
# LegalSourceClient 聚合（含 pkulaw 子源）
# ---------------------------------------------------------------------------

class TestLegalSourceClientWithPkulaw:
    def test_pkulaw_law_included(self):
        """all 检索：国家库 + 北大法宝法条 合并返回。"""
        client = LegalSourceClient.__new__(LegalSourceClient)
        client.national_law = MagicMock()
        client.national_law.is_available.return_value = True
        client.national_law.search_law.return_value = [
            {"title": "民诉法", "url": "https://flk/1", "content": "", "source": "national_law_db"}
        ]
        client.court_case = MagicMock()
        client.court_case.is_available.return_value = False
        client.xbg = MagicMock()
        client.xbg.is_available.return_value = False
        client.pkulaw = PkulawLegalClient(FakePkulawClient())
        data = client.search("民法典", source_type="all")
        sources = {r["source"] for r in data["results"]}
        assert SOURCE_PKULAW in sources
        assert "national_law_db" in sources

    def test_pkulaw_failure_does_not_block_others(self):
        """北大法宝失败 → 归入 errors，但不阻断国家库结果（F9 容错）。"""
        client = LegalSourceClient.__new__(LegalSourceClient)
        client.national_law = MagicMock()
        client.national_law.is_available.return_value = True
        client.national_law.search_law.return_value = [
            {"title": "民诉法", "url": "https://flk/1", "content": "", "source": "national_law_db"}
        ]
        client.court_case = MagicMock()
        client.court_case.is_available.return_value = False
        client.xbg = MagicMock()
        client.xbg.is_available.return_value = False
        # 整支北大法宝子源抛错（law 检索），source_type=law 不触发 case 分支
        bad = MagicMock()
        bad.is_available.return_value = True
        bad.search_law.side_effect = RuntimeError("pkulaw 超时")
        client.pkulaw = bad
        data = client.search("民法典", source_type="law")
        assert data["count"] == 1
        assert any("pkulaw 超时" in e for e in data["errors"])


# ---------------------------------------------------------------------------
# 融合验证状态
# ---------------------------------------------------------------------------

def _legal(title, url, sub=SOURCE_PKULAW, **extra):
    return {"title": title, "url": url, "content": "", "source": sub, **extra}


class TestFusionPkulaw:
    def test_pkulaw_marked_verified_official(self):
        fused = fuse_evidence([], [], [_legal("民法典", "https://pkulaw.com/1")])
        assert fused["count"] == 1
        assert fused["sources"][0]["verification"] == VERIFIED_OFFICIAL
        assert fused["sources"][0]["sub_source"] == SOURCE_PKULAW


# ---------------------------------------------------------------------------
# ReAct 工具封装
# ---------------------------------------------------------------------------

class TestPkulawSearchTool:
    def _spec(self, client=None):
        return build_pkulaw_search_spec(client or FakePkulawClient())

    def test_article_search_ok(self):
        result = self._spec().executor("article_search", query="离婚冷静期")
        assert result.ok is True
        assert result.source == SOURCE_LEGAL
        assert result.data["count"] == 1

    def test_case_search_ok(self):
        result = self._spec().executor("case_search", query="买卖合同纠纷")
        assert result.ok is True
        assert result.data["results"][0]["case_number"]

    def test_unavailable_normalized(self):
        result = self._spec(FakePkulawClient(available=False)).executor("article_search", query="x")
        assert result.ok is False
        assert result.summary.startswith("法宝检索失败")

    def test_budget_exhausted(self, monkeypatch):
        """预算用尽 → ok=False 首词'法宝额度已用尽'，不抛出。

        预算埋点在 PkulawMCPClient._run（真实客户端路径）；用真实客户端并
        mock 其 is_available 与 _run，使工具走到 BudgetExceededError 分支。
        """
        from src.observability.cost_budget import BudgetExceededError as _BE

        real = PkulawMCPClient()
        monkeypatch.setattr(real, "is_available", lambda: True)
        monkeypatch.setattr(real, "_run", lambda purpose, args: (_ for _ in ()).throw(_BE("pkulaw", 200, 200)))
        result = build_pkulaw_search_spec(real).executor("article_search", query="x")
        assert result.ok is False
        assert result.summary.startswith("法宝额度已用尽")

    def test_schema_has_mode_enum(self):
        schema = self._spec().to_openai_format()
        assert schema["function"]["name"] == "pkulaw_search"
        assert "mode" in schema["function"]["parameters"]["properties"]


class TestPkulawVerifyTool:
    def _spec(self, client=None):
        return build_pkulaw_verify_spec(client or FakePkulawClient())

    def test_law_name_ok(self):
        result = self._spec().executor("law_name", text="见《民法典》第一条")
        assert result.ok is True
        assert result.data["mode"] == "law_name"

    def test_add_links_returns_linked(self):
        result = self._spec().executor("add_links", text="依据民法典第一条")
        assert result.ok is True
        assert "pkulaw.com" in result.data["linked"]

    def test_budget_exhausted(self, monkeypatch):
        from src.observability.cost_budget import BudgetExceededError as _BE

        real = PkulawMCPClient()
        monkeypatch.setattr(real, "is_available", lambda: True)
        monkeypatch.setattr(real, "_run", lambda purpose, args: (_ for _ in ()).throw(_BE("pkulaw", 200, 200)))
        result = build_pkulaw_verify_spec(real).executor("law_name", text="x")
        assert result.ok is False
        assert result.summary.startswith("法宝额度已用尽")


# ---------------------------------------------------------------------------
# build_default_tools 注册
# ---------------------------------------------------------------------------

class TestRegistryIntegrationPkulaw:
    def test_pkulaw_registered_when_available(self, monkeypatch):
        """PKULAW_ENABLED=true 且客户端可用 → 注册 pkulaw_search / pkulaw_verify。"""
        monkeypatch.setattr("src.agents.tools.PKULAW_ENABLED", True)
        registry = build_default_tools(FakeRetriever(), pkulaw_client=FakePkulawClient())
        names = {t.name for t in registry.list_tools()}
        assert "pkulaw_search" in names
        assert "pkulaw_verify" in names

    def test_pkulaw_not_registered_when_unavailable(self, monkeypatch):
        """客户端不可用 → 跳过注册（不阻断其他工具）。"""
        monkeypatch.setattr("src.agents.tools.PKULAW_ENABLED", True)
        registry = build_default_tools(FakeRetriever(), pkulaw_client=FakePkulawClient(available=False))
        names = {t.name for t in registry.list_tools()}
        assert "pkulaw_search" not in names
        assert "pkulaw_verify" not in names

    def test_pkulaw_disabled(self, monkeypatch):
        monkeypatch.setattr("src.agents.tools.PKULAW_ENABLED", False)
        registry = build_default_tools(FakeRetriever(), pkulaw_client=FakePkulawClient())
        names = {t.name for t in registry.list_tools()}
        assert "pkulaw_search" not in names
