"""
M1 工具层测试：ToolSpec / ToolRegistry / retrieve_knowledge / web_search / TavilySearchClient。

不依赖外部服务：retriever 用 FakeRetriever，Tavily 用 mock client。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.agents.tools.base import (
    SOURCE_INTERNAL_KB,
    SOURCE_WEB,
    ToolResult,
    ToolSpec,
    truncate_summary,
)
from src.agents.tools.registry import ToolRegistry
from src.agents.tools.retrieve_knowledge import RetrieveKnowledgeTool
from src.agents.tools.web_search import WebSearchTool
from src.search.tavily import TavilySearchClient


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------

class TestToolSpec:
    def test_to_openai_format(self):
        spec = ToolSpec(
            name="my_tool",
            description="测试工具",
            parameters={"query": {"type": "string", "description": "查询"}},
            required=["query"],
        )
        schema = spec.to_openai_format()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "my_tool"
        assert fn["description"] == "测试工具"
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"]["properties"]["query"]["type"] == "string"
        assert fn["parameters"]["required"] == ["query"]


class TestTruncateSummary:
    def test_truncate_long(self):
        text = "长" * 500
        out = truncate_summary(text, max_chars=300)
        assert len(out) <= 300
        assert out.endswith("…")

    def test_short_passthrough(self):
        assert truncate_summary("短文本") == "短文本"


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_register_and_list(self):
        reg = ToolRegistry()
        spec = ToolSpec(name="a", description="A", parameters={}, executor=lambda: ToolResult(tool="a", call_id="", ok=True, summary="ok"))
        reg.register(spec)
        assert reg.has("a")
        assert reg.get("a") is spec
        assert len(reg.list_tools()) == 1
        assert len(reg.to_openai_schemas()) == 1

    def test_duplicate_register_raises(self):
        reg = ToolRegistry()
        spec = ToolSpec(name="a", description="A", parameters={})
        reg.register(spec)
        with pytest.raises(ValueError, match="已注册"):
            reg.register(ToolSpec(name="a", description="B", parameters={}))

    def test_execute_ok(self):
        reg = ToolRegistry()

        def _exec(query: str) -> ToolResult:
            return ToolResult(tool="echo", call_id="", ok=True, summary=f"收到: {query}")

        reg.register(ToolSpec(name="echo", description="", parameters={"query": {"type": "string"}}, required=["query"], executor=_exec))
        result = reg.execute("echo", {"query": "你好"}, call_id="call_1")
        assert result.ok
        assert result.call_id == "call_1"  # call_id 由调用方回填
        assert result.summary == "收到: 你好"

    def test_execute_unknown_tool(self):
        reg = ToolRegistry()
        result = reg.execute("nope", {}, call_id="call_1")
        assert not result.ok
        assert result.summary.startswith("未知工具")

    def test_execute_param_error(self):
        """参数与 executor 签名不匹配 → 参数校验失败（共享约定：summary 首词为错误标签）"""
        reg = ToolRegistry()

        def _exec(query: str) -> ToolResult:
            return ToolResult(tool="x", call_id="", ok=True, summary="ok")

        reg.register(ToolSpec(name="x", description="", parameters={"query": {"type": "string"}}, required=["query"], executor=_exec))
        result = reg.execute("x", {"unknown_arg": 1}, call_id="c1")
        assert not result.ok
        assert result.summary.startswith("参数校验失败")

    def test_execute_exception_normalized(self):
        """工具抛异常 → 归一化为 ok=False，不向上抛出"""
        reg = ToolRegistry()

        def _boom() -> ToolResult:
            raise RuntimeError("内部爆炸")

        reg.register(ToolSpec(name="boom", description="", parameters={}, executor=_boom))
        result = reg.execute("boom", {})
        assert not result.ok
        assert result.summary.startswith("工具执行失败")


# ---------------------------------------------------------------------------
# RetrieveKnowledgeTool
# ---------------------------------------------------------------------------

class TestRetrieveKnowledgeTool:
    def test_build_spec(self, fake_retriever):
        tool = RetrieveKnowledgeTool(fake_retriever)
        spec = tool.build_spec()
        assert spec.name == "retrieve_knowledge"
        assert "query" in spec.parameters
        assert spec.required == ["query"]

    def test_exec_ok(self, fake_retriever):
        tool = RetrieveKnowledgeTool(fake_retriever, default_top_k=5)
        result = tool._exec(query="测试")
        assert result.ok
        assert result.source == SOURCE_INTERNAL_KB
        assert "检索到 1 条相关法条" in result.summary
        assert result.data["count"] == 1
        assert result.data["docs"][0]["law_name"] == "《测试法》"
        assert len(result.summary) <= 300

    def test_exec_retriever_error(self, fake_retriever):
        fake_retriever.search = MagicMock(side_effect=RuntimeError("pg down"))
        tool = RetrieveKnowledgeTool(fake_retriever)
        result = tool._exec(query="测试")
        assert not result.ok
        assert result.summary.startswith("检索失败")

    def test_top_k_bounds(self, fake_retriever):
        tool = RetrieveKnowledgeTool(fake_retriever, default_top_k=5)
        result = tool._exec(query="测试", top_k=999)
        assert result.ok
        assert result.data["count"] <= 20


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class TestWebSearchTool:
    def _mock_client(self, available=True, results=None):
        client = MagicMock(spec=TavilySearchClient)
        client.is_available.return_value = available
        client.search.return_value = results or [
            {"title": "民事诉讼法修订", "url": "http://example.com/x", "content": "内容", "score": 0.9}
        ]
        return client

    def test_build_spec(self):
        tool = WebSearchTool(self._mock_client())
        spec = tool.build_spec()
        assert spec.name == "web_search"
        assert spec.required == ["query"]

    def test_exec_ok(self):
        tool = WebSearchTool(self._mock_client())
        result = tool._exec(query="民事诉讼法 最新修订")
        assert result.ok
        assert result.source == SOURCE_WEB
        assert "搜索到 1 条网络结果" in result.summary
        assert result.data["count"] == 1

    def test_exec_unavailable(self):
        """未配置 TAVILY_API_KEY → 搜索不可用（REQ-UW1）"""
        tool = WebSearchTool(self._mock_client(available=False))
        result = tool._exec(query="测试")
        assert not result.ok
        assert result.summary.startswith("搜索不可用")

    def test_exec_search_error(self):
        client = self._mock_client()
        client.search.side_effect = RuntimeError("timeout")
        tool = WebSearchTool(client)
        result = tool._exec(query="测试")
        assert not result.ok
        assert result.summary.startswith("搜索不可用")


# ---------------------------------------------------------------------------
# TavilySearchClient
# ---------------------------------------------------------------------------

class TestTavilySearchClient:
    def test_unavailable_without_key(self):
        client = TavilySearchClient(api_key="")
        assert not client.is_available()

    @staticmethod
    def _patch_tavily_module(fake_client):
        """伪造 sys.modules['tavily']，使 src.search.tavily 延迟导入拿到假 TavilyClient。"""
        import sys
        fake_tavily = MagicMock()
        fake_tavily.TavilyClient.return_value = fake_client
        return patch.dict(sys.modules, {"tavily": fake_tavily})

    def test_search_parses_results(self):
        fake_client = MagicMock()
        fake_client.search.return_value = {
            "results": [
                {"title": "T1", "url": "http://a", "content": "C1", "score": 0.8},
                {"title": "T2", "url": "http://b", "content": "C2", "score": 0.5},
            ]
        }
        with self._patch_tavily_module(fake_client):
            client = TavilySearchClient(api_key="tvly-test")
        assert client.is_available()
        results = client.search("查询", max_results=5)
        assert len(results) == 2
        assert results[0]["title"] == "T1"
        assert results[0]["score"] == 0.8

    def test_search_exception_normalized(self):
        fake_client = MagicMock()
        fake_client.search.side_effect = RuntimeError("boom")
        with self._patch_tavily_module(fake_client):
            client = TavilySearchClient(api_key="tvly-test")
        with pytest.raises(RuntimeError, match="Tavily 搜索失败"):
            client.search("查询")
