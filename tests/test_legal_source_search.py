"""
M2 官方法律源测试（F9）：legal_source_search 工具、LegalSourceClient 聚合逻辑。

全部 mock HTTP，不打真实网络（AGENTS.md 禁止事项）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.agents.tools import build_default_tools
from src.agents.tools.legal_source_search import build_legal_source_search_spec
from src.agents.tools.base import SOURCE_LEGAL
from src.search.legal_sources import (
    SOURCE_COURT_CASE_LIB,
    SOURCE_NATIONAL_LAW_DB,
    SOURCE_XBG,
    CourtCaseLibraryClient,
    LegalSourceClient,
    NationalLawClient,
    XiaobaogongClient,
)
from tests.fakes import FakeRetriever


# ---------------------------------------------------------------------------
# NationalLawClient
# ---------------------------------------------------------------------------

class TestNationalLawClient:
    def test_search_law_parses_records(self):
        """flk 接口返回 rows → 统一条目（title/url/状态/发布机关）。"""
        payload = {
            "total": 1,
            "rows": [
                {
                    "bbbs": "ff8081818a21dc13018b425303b7086d",
                    "title": "中华人民共和国<em class='highlight'>民事诉讼法</em>",
                    "gbrq": "2023-09-01",
                    "sxrq": "2024-01-01",
                    "sxx": 3,
                    "zdjgName": "全国人民代表大会",
                    "flxz": "法律",
                    "score": 14.93,
                },
            ],
        }
        with patch("src.search.legal_sources.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = payload
            client = NationalLawClient()
            results = client.search_law("民事诉讼法")
        assert len(results) == 1
        r = results[0]
        assert r["source"] == SOURCE_NATIONAL_LAW_DB
        assert r["title"] == "中华人民共和国民事诉讼法"  # <em> 标签已清除
        assert r["url"].startswith("https://flk.npc.gov.cn/detail2.html?bbbs=")
        assert r["law_status"] == "现行有效"
        assert r["office"] == "全国人民代表大会"
        assert r["publish_date"] == "2023-09-01"
        assert r["effective_date"] == "2024-01-01"

    def test_search_law_http_error_raises_runtime(self):
        """接口不可达 → RuntimeError（由工具层归一化为 ok=False）。"""
        with patch("src.search.legal_sources.requests.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError):
                NationalLawClient().search_law("测试")

    def test_search_law_empty_keyword_no_crash(self):
        """空关键词接口返回空 rows → 空列表。"""
        payload = {"total": 0, "rows": []}
        with patch("src.search.legal_sources.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = payload
            assert NationalLawClient().search_law("") == []


# ---------------------------------------------------------------------------
# CourtCaseLibraryClient（Tavily 域限定）
# ---------------------------------------------------------------------------

class TestCourtCaseLibraryClient:
    def test_unavailable_without_tavily(self):
        """未配置 Tavily → is_available()=False，search 抛 RuntimeError。"""
        client = CourtCaseLibraryClient(tavily_client=None)
        assert client.is_available() is False
        with pytest.raises(RuntimeError):
            client.search_case("合同纠纷")

    def test_filters_non_official_domain(self):
        """域限定搜索：仅保留 anli.court.gov.cn 域内结果。"""
        tavily = MagicMock()
        tavily.is_available.return_value = True
        tavily.search.return_value = [
            {"title": "某案例", "url": "https://anli.court.gov.cn/#/case/1", "content": "c", "score": 0.8},
            {"title": "无关", "url": "https://example.com/foo", "content": "x", "score": 0.9},
        ]
        client = CourtCaseLibraryClient(tavily_client=tavily)
        results = client.search_case("合同纠纷")
        assert len(results) == 1
        assert results[0]["source"] == SOURCE_COURT_CASE_LIB
        # 底层查询带 site: 限定
        assert "anli.court.gov.cn" in tavily.search.call_args[0][0]


# ---------------------------------------------------------------------------
# XiaobaogongClient
# ---------------------------------------------------------------------------

class TestXiaobaogongClient:
    def test_unavailable_without_config(self):
        assert XiaobaogongClient(api_key="", api_url="").is_available() is False

    def test_search_parses_results(self):
        with patch("src.search.legal_sources.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "results": [{"title": "张三诉李四案", "url": "https://xbg.example/c/1", "summary": "判决要点"}]
            }
            client = XiaobaogongClient(api_key="k", api_url="https://xbg.example/api")
            results = client.search_case("民间借贷")
        assert len(results) == 1
        assert results[0]["source"] == SOURCE_XBG


# ---------------------------------------------------------------------------
# LegalSourceClient 聚合
# ---------------------------------------------------------------------------

class TestLegalSourceClientAggregate:
    def _client(self, law_results=None, case_results=None, law_error=None, law_available=True):
        client = LegalSourceClient.__new__(LegalSourceClient)
        client.national_law = MagicMock()
        client.national_law.is_available.return_value = law_available
        if law_error:
            client.national_law.search_law.side_effect = RuntimeError(law_error)
        else:
            client.national_law.search_law.return_value = law_results or []
        client.court_case = MagicMock()
        client.court_case.is_available.return_value = bool(case_results)
        client.court_case.search_case.return_value = case_results or []
        client.xbg = MagicMock()
        client.xbg.is_available.return_value = False
        return client

    def test_law_only_search(self):
        law = [{"title": "民法典", "url": "https://flk.npc.gov.cn/1", "content": "", "source": SOURCE_NATIONAL_LAW_DB}]
        data = self._client(law_results=law).search("民法典", source_type="law")
        assert data["count"] == 1
        assert data["sources"] == [SOURCE_NATIONAL_LAW_DB]

    def test_law_failure_does_not_block_cases(self):
        """法规库失败 + 案例库有结果 → 案例正常返回，errors 记录失败（F9 容错）。"""
        case = [{"title": "某案", "url": "https://anli.court.gov.cn/1", "content": "", "source": SOURCE_COURT_CASE_LIB}]
        data = self._client(law_error="接口超时", case_results=case).search("测试", source_type="all")
        assert data["count"] == 1
        assert any("接口超时" in e for e in data["errors"])

    def test_all_sources_fail_raises(self):
        with pytest.raises(RuntimeError):
            self._client(law_error="失败A").search("测试", source_type="all")

    def test_empty_query_raises(self):
        with pytest.raises(RuntimeError):
            LegalSourceClient().search("  ")

    def test_dedup_by_url(self):
        law = [
            {"title": "民法典", "url": "https://flk.npc.gov.cn/1", "content": "", "source": SOURCE_NATIONAL_LAW_DB},
            {"title": "民法典（重复）", "url": "https://flk.npc.gov.cn/1", "content": "", "source": SOURCE_NATIONAL_LAW_DB},
        ]
        data = self._client(law_results=law).search("民法典", source_type="law")
        assert data["count"] == 1


# ---------------------------------------------------------------------------
# LegalSourceSearchTool（工具封装）
# ---------------------------------------------------------------------------

class TestLegalSourceSearchTool:
    def _spec(self, client):
        return build_legal_source_search_spec(client)

    def test_ok_result(self):
        client = MagicMock()
        client.is_available.return_value = True
        client.search.return_value = {
            "results": [{"title": "民事诉讼法", "url": "https://flk/1", "content": "", "source": SOURCE_NATIONAL_LAW_DB, "law_status": "现行有效"}],
            "count": 1,
            "sources": [SOURCE_NATIONAL_LAW_DB],
            "errors": [],
        }
        result = self._spec(client).executor("民事诉讼法")
        assert result.ok is True
        assert result.source == SOURCE_LEGAL
        assert "现行有效" in result.summary
        assert len(result.summary) <= 300

    def test_unavailable_config(self):
        """全部官方源未配置 → ok=False，首词"权威源检索失败"。"""
        client = MagicMock()
        client.is_available.return_value = False
        result = self._spec(client).executor("测试")
        assert result.ok is False
        assert result.summary.startswith("权威源检索失败")

    def test_search_exception_normalized(self):
        """检索异常 → ok=False 首词"权威源检索失败"，不抛出。"""
        client = MagicMock()
        client.is_available.return_value = True
        client.search.side_effect = RuntimeError("全部子源失败")
        result = self._spec(client).executor("测试")
        assert result.ok is False
        assert result.summary.startswith("权威源检索失败")

    def test_invalid_source_type_defaults_to_law(self):
        client = MagicMock()
        client.is_available.return_value = True
        client.search.return_value = {"results": [], "count": 0, "sources": [], "errors": []}
        self._spec(client).executor("测试", source_type="bogus")
        assert client.search.call_args.kwargs.get("source_type") == "law"

    def test_spec_schema(self):
        client = MagicMock()
        spec = self._spec(client)
        assert spec.name == "legal_source_search"
        schema = spec.to_openai_format()
        assert schema["function"]["name"] == "legal_source_search"
        assert "query" in schema["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# build_default_tools 注册集成
# ---------------------------------------------------------------------------

class TestRegistryIntegration:
    def test_legal_source_registered_by_default(self, monkeypatch):
        """默认注册三个工具：retrieve_knowledge + web_search + legal_source_search。"""
        monkeypatch.setattr("src.agents.tools.LEGAL_SOURCE_ENABLED", True)
        registry = build_default_tools(FakeRetriever())
        names = [t.name for t in registry.list_tools()]
        assert "legal_source_search" in names
        assert len(names) == 3

    def test_legal_source_disabled(self, monkeypatch):
        """LEGAL_SOURCE_ENABLED=false → 不注册（回退 M1 行为）。"""
        monkeypatch.setattr("src.agents.tools.LEGAL_SOURCE_ENABLED", False)
        registry = build_default_tools(FakeRetriever())
        names = [t.name for t in registry.list_tools()]
        assert "legal_source_search" not in names
        assert len(names) == 2
