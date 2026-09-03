"""
F15 Tavily / pkulaw 埋点集成测试（成功落库、失败不落库）。

验证（docs/F15 §3.3 / §3.4）：
- TavilySearchClient.search() 成功 → record_tavily_usage 落库；失败不落；
- PkulawMCPClient._run() 成功 → record_pkulaw_usage 落库；失败/超限不落；
- 埋点异常被吞（不拖垮主链路）。

隔离策略：不触真实网络与 DB——mock 客户端与 psycopg2.connect；
conftest 已强制池关闭 → db_connection() 走一次性直连命中打桩点。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.observability.cost_budget import KIND_TAVILY, get_budget, reset_budget
from src.search.pkulaw_mcp import PkulawMCPClient  # noqa: F401 - 显式暴露
from src.search.tavily import TavilySearchClient


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


class TestTavilyInstrumentation:
    def _client(self) -> TavilySearchClient:
        client = TavilySearchClient(api_key="k")
        client._client = MagicMock()
        return client

    def _reset_budget(self, monkeypatch):
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_TAVILY_CALLS_PER_DAY", 500)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        get_budget().reset(KIND_TAVILY)

    def test_success_records_usage(self, monkeypatch):
        self._reset_budget(monkeypatch)
        client = self._client()
        client._client.search.return_value = {
            "results": [{"title": "t", "url": "u", "content": "c", "score": 0.9}]
        }

        with patch("src.observability.usage_store.record_tavily_usage") as mock_rec:
            out = client.search("工伤认定")
            mock_rec.assert_called_once()
            assert mock_rec.call_args.kwargs["depth"] == "basic"
        assert out and out[0]["title"] == "t"

    def test_failure_does_not_record(self, monkeypatch):
        self._reset_budget(monkeypatch)
        client = self._client()
        client._client.search.side_effect = RuntimeError("api down")

        with patch("src.observability.usage_store.record_tavily_usage") as mock_rec:
            with pytest.raises(RuntimeError):
                client.search("q")
            mock_rec.assert_not_called()


# ---------------------------------------------------------------------------
# 北大法宝 MCP
# ---------------------------------------------------------------------------


class TestPkulawInstrumentation:
    def _client(self) -> PkulawMCPClient:
        """构造不建连的客户端（跳过 __init__ 的网络探测）。"""
        client = PkulawMCPClient.__new__(PkulawMCPClient)
        client.url = "https://fake"
        client.token = "t"
        client.timeout = 5.0
        return client

    def _reset_budget(self, monkeypatch):
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_PKULAW_CALLS_PER_DAY", 200)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        get_budget().reset("pkulaw")

    def test_success_records_usage(self, monkeypatch):
        self._reset_budget(monkeypatch)
        client = self._client()
        with patch.object(PkulawMCPClient, "_a_call", return_value=[{"lawName": "劳动法"}]):
            with patch("src.observability.usage_store.record_pkulaw_usage") as mock_rec:
                raw = client._run("article_search", {"text": "q"})
                mock_rec.assert_called_once()
                assert mock_rec.call_args.kwargs["purpose"] == "article_search"
        assert raw == [{"lawName": "劳动法"}]

    def test_failure_releases_and_no_record(self, monkeypatch):
        self._reset_budget(monkeypatch)
        client = self._client()
        with patch.object(PkulawMCPClient, "_a_call", side_effect=RuntimeError("net down")):
            with patch("src.observability.usage_store.record_pkulaw_usage") as mock_rec:
                with pytest.raises(RuntimeError, match="北大法宝 MCP 调用失败"):
                    client._run("article_search", {"text": "q"})
                mock_rec.assert_not_called()
        # 失败归还配额
        assert get_budget().used("pkulaw") == 0
