"""
QA 独立验证补充测试（M1 边界/薄弱点，由 QA 工程师严过关新增）。

覆盖（对应 PRD AC-1/2/5/6/7 与架构设计 §8 共享约定）：
1. ToolRegistry.execute 非法参数 / 未知工具 / executor 异常 → ToolResult(ok=False)
   且 summary 首词为错误标签，绝不向上抛出（共享约定 §8.3）。
2. truncate_summary 300 字符截断边界（正好 300 / 超过 / 中文多字节 / 空值）。
3. ReAct 循环：agent_turns 达到默认 max=5 时移除 tools 强制产出答案（REQ-UW4）；
   parallel_tool_calls 多个 tool_calls 被遍历执行（DeepSeek V4 特性，R5）。
4. FailoverLLMBackend：4xx（403/404/401 认证）→ 降级并重放；
   429/5xx/408 → 不降级；创建期缺主后端 → 直接降级。
5. web_search：Tavily 异常/超时/无 Key → ok=False 且 summary 首词"搜索不可用"（REQ-UW1），
   不抛异常；max_results 边界收敛到 [1, 10]。
6. graph：AGENT_REACT_ENABLED=false 时回退固定管线（AC-7 向后兼容），
   SSE 不产出 tool_call/tool_result，旧事件流不受破坏。

全部为本地单测：retriever 用 FakeRetriever，LLM 用 FakeToolLLM，Tavily 用 MagicMock。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.graph import LawAgentGraph
from src.agents.tools import build_default_tools
from src.agents.tools.base import (
    SOURCE_INTERNAL_KB,
    SOURCE_WEB,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
    truncate_summary,
)
from src.agents.tools.registry import ToolRegistry
from src.agents.tools.web_search import WebSearchTool
from src.llm.base import LLMBackend, ToolCall, ToolCallResponse
from src.llm.failover import FailoverLLMBackend
from src.search.tavily import TavilySearchClient
from tests.fakes import FakeRetriever, FakeToolLLM


# ---------------------------------------------------------------------------
# 公共构造
# ---------------------------------------------------------------------------

def _tool_call_response(query="测试") -> ToolCallResponse:
    return ToolCallResponse(
        content="",
        tool_calls=[ToolCall(id="call_1", name="retrieve_knowledge", arguments={"query": query})],
        raw={},
    )


def _final_response(text="根据《测试法》第一条，测试规定内容。") -> ToolCallResponse:
    return ToolCallResponse(content=text, tool_calls=[], raw={})


def _build_agent(llm, retriever=None, max_tool_turns=5, monkeypatch=None, react=True):
    if monkeypatch is not None:
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", react)
        monkeypatch.setattr("src.agents.graph.AGENT_MAX_TOOL_TURNS", max_tool_turns)
    retriever = retriever or FakeRetriever()
    registry = build_default_tools(retriever)
    agent = LawAgentGraph(
        retriever=retriever, llm=llm,
        top_k=3, max_retries=0,
        memory_manager=None, faq_cache=None, query_logger=None,
        registry=registry,
    )
    return agent


# ---------------------------------------------------------------------------
# 1. ToolRegistry.execute 边界
# ---------------------------------------------------------------------------

class TestRegistryExecuteEdge:
    def test_execute_no_executor(self):
        """executor 未注册 → ok=False + 首词"工具执行器未注册"（不抛异常）。"""
        reg = ToolRegistry()
        reg.register(ToolSpec(name="noexec", description="", parameters={}))
        result = reg.execute("noexec", {}, call_id="c1")
        assert isinstance(result, ToolResult)
        assert not result.ok
        assert result.summary.startswith("工具执行器未注册")
        assert result.call_id == "c1"

    def test_execute_non_toolresult_wrapped(self):
        """executor 返回非 ToolResult（防御包装）→ ok=True 且 summary 为该值。"""
        reg = ToolRegistry()
        reg.register(ToolSpec(name="strret", description="", parameters={}, executor=lambda: "原始字符串"))
        result = reg.execute("strret", {}, call_id="c1")
        assert isinstance(result, ToolResult)
        assert result.ok
        assert result.summary == "原始字符串"

    def test_execute_tool_execution_error_normalized(self):
        """executor 抛 ToolExecutionError → ok=False + 首词"工具执行失败"（不抛异常）。"""
        reg = ToolRegistry()

        def _boom() -> ToolResult:
            raise ToolExecutionError("内部工具错误")

        reg.register(ToolSpec(name="boom", description="", parameters={}, executor=_boom))
        result = reg.execute("boom", {}, call_id="c1")
        assert isinstance(result, ToolResult)
        assert not result.ok
        assert result.summary.startswith("工具执行失败")

    def test_execute_never_raises_for_any_input(self):
        """未知工具 / 参数错误 / 执行异常 三种路径均不向调用方抛出。"""
        reg = ToolRegistry()

        def _boom(query: str) -> ToolResult:  # 固定签名：传错参数才会触发 TypeError
            raise RuntimeError("任意运行时错误")

        reg.register(ToolSpec(name="boom", description="", parameters={"query": {"type": "string"}}, required=["query"], executor=_boom))
        results = [
            reg.execute("unknown_tool", {}, call_id="c1"),   # 未知工具
            reg.execute("boom", {"bad_arg": 1}, call_id="c2"),  # 参数不匹配 → TypeError
            reg.execute("boom", {"query": "x"}, call_id="c3"),  # 参数正确 → executor 内部异常
        ]
        for r in results:
            assert isinstance(r, ToolResult)
            assert r.ok is False
        assert results[0].summary.startswith("未知工具")
        assert results[1].summary.startswith("参数校验失败")
        assert results[2].summary.startswith("工具执行失败")


# ---------------------------------------------------------------------------
# 2. truncate_summary 300 字符截断边界
# ---------------------------------------------------------------------------

class TestTruncateSummaryEdge:
    def test_exact_300_passthrough(self):
        text = "x" * 300
        assert truncate_summary(text, max_chars=300) == text
        assert len(truncate_summary(text, max_chars=300)) == 300

    def test_301_truncated_to_300(self):
        out = truncate_summary("x" * 301, max_chars=300)
        assert len(out) == 300
        assert out.endswith("…")
        assert out == "x" * 299 + "…"

    def test_chinese_300_passthrough(self):
        """中文按字符计数：300 个汉字不截断（不按 UTF-8 字节数误截）。"""
        text = "法" * 300
        assert truncate_summary(text, max_chars=300) == text

    def test_chinese_301_truncated(self):
        out = truncate_summary("法" * 301, max_chars=300)
        assert len(out) == 300
        assert out.endswith("…")
        assert out == "法" * 299 + "…"

    def test_empty_and_none(self):
        assert truncate_summary("", max_chars=300) == ""
        assert truncate_summary(None, max_chars=300) == ""

    def test_strips_whitespace_before_truncate(self):
        assert truncate_summary("  短文本  ", max_chars=300) == "短文本"
        out = truncate_summary("  " + "x" * 300 + "  ", max_chars=300)
        assert len(out) == 300

    def test_toolresult_summary_auto_truncated(self):
        """ToolResult 构造时 summary 自动截断到 ≤300（SSE 事件体不膨胀）。"""
        r = ToolResult(tool="t", call_id="c", ok=True, summary="长" * 500)
        assert len(r.summary) <= 300
        assert r.summary.endswith("…")


# ---------------------------------------------------------------------------
# 3. ReAct 循环边界（REQ-UW4 / R5 parallel_tool_calls）
# ---------------------------------------------------------------------------

class TestReactLoopEdge:
    def test_default_max_turns_forces_answer(self, monkeypatch):
        """默认 max=5：无限工具调用 → 强制产出答案，agent_turns ≤ max+1（REQ-UW4）。"""
        llm = FakeToolLLM([_tool_call_response()] * 20)  # 永不自然收敛
        agent = _build_agent(llm, monkeypatch=monkeypatch)  # 不覆盖 max → 默认 5
        result = agent.ask("行政拘留最长多久")
        assert result["answer"], "达到上限后必须产出答案"
        assert result["agent_turns"] <= 6, f"agent_turns={result['agent_turns']} 超出 max+1"

    def test_tools_removed_at_max_turn(self, monkeypatch):
        """达到上限后 LLM 收到的 tools 为空（schemas 移除，强制走最终答案分支）。"""
        monkeypatch.setattr("src.agents.graph.AGENT_MAX_TOOL_TURNS", 2)
        llm = FakeToolLLM([_tool_call_response()] * 10)
        agent = _build_agent(llm, max_tool_turns=2, monkeypatch=monkeypatch)
        agent.ask("行政拘留最长多久")
        # 前 2 轮（turns=0,1）有工具；第 3 轮（turns=2 达上限）tools 被移除
        assert llm.calls[0]["tools"], "第 1 轮应带工具 schema"
        assert llm.calls[1]["tools"], "第 2 轮应带工具 schema"
        assert llm.calls[-1]["tools"] == [], "达上限轮 tools 必须为空（REQ-UW4）"

    def test_parallel_tool_calls_all_executed(self, monkeypatch):
        """一次返回多个 tool_calls → tools 节点遍历执行全部（DeepSeek V4 特性，R5）。"""
        llm = FakeToolLLM([
            ToolCallResponse(
                content="",
                tool_calls=[
                    ToolCall(id="p1", name="retrieve_knowledge", arguments={"query": "问题A"}),
                    ToolCall(id="p2", name="web_search", arguments={"query": "问题B"}),
                ],
                raw={},
            ),
            _final_response("并行工具后回答"),
        ])
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert len(result["tool_log"]) == 2, "两个 tool_calls 必须都被执行"
        tools_called = {entry["tool"] for entry in result["tool_log"]}
        assert tools_called == {"retrieve_knowledge", "web_search"}

    def test_parallel_tool_calls_sse_events(self, monkeypatch):
        """并行调用 → SSE 产出 2 个 tool_call + 2 个 tool_result（F4 契约）。"""
        llm = FakeToolLLM([
            ToolCallResponse(
                content="",
                tool_calls=[
                    ToolCall(id="p1", name="retrieve_knowledge", arguments={"query": "问题A"}),
                    ToolCall(id="p2", name="web_search", arguments={"query": "问题B"}),
                ],
                raw={},
            ),
            _final_response("并行回答"),
        ])
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        events = list(agent.stream("行政拘留最长多久"))
        tool_call_events = [e for e in events if e["type"] == "tool_call"]
        tool_result_events = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_call_events) == 2
        assert len(tool_result_events) == 2
        # web_search 未配置 Key → ok=False + "搜索不可用"（REQ-UW1）
        ws_result = next(e for e in tool_result_events if e["tool"] == "web_search")
        assert ws_result["ok"] is False
        assert ws_result["summary"].startswith("搜索不可用")


# ---------------------------------------------------------------------------
# 4. FailoverLLMBackend 边界
# ---------------------------------------------------------------------------

class FakeAPIError(Exception):
    """带 HTTP 状态码的假 API 异常（模拟 openai SDK 异常形态）。"""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"API error {status_code}")


class _FakePrimary(LLMBackend):
    def __init__(self, error: Exception | None = None):
        super().__init__(model="primary-model")
        self.error = error

    def _generate_impl(self, messages):
        if self.error:
            raise self.error
        return "primary-answer"

    def _stream_impl(self, messages):
        yield "primary-token"

    def get_context_window(self):
        return 1000

    def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
        if self.error:
            raise self.error
        return ToolCallResponse(content="primary-tools", tool_calls=[])


class _FakeOllamaBackend(LLMBackend):  # 类名含 Ollama，便于 active_backend 标签断言
    def __init__(self):
        super().__init__(model="qwen2.5:3b")
        self.tools_args: list = []

    def _generate_impl(self, messages):
        return "fallback-answer"

    def _stream_impl(self, messages):
        yield "fallback-token"

    def get_context_window(self):
        return 32000

    def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
        self.tools_args.append((messages, tools, tool_choice))
        return ToolCallResponse(content="fallback-tools", tool_calls=[])


class TestFailoverEdge:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_4xx_business_auth_degrades(self, status):
        """4xx（除 408/429）→ 降级到备用后端并重放。"""
        primary = _FakePrimary(error=FakeAPIError(status))
        backend = FailoverLLMBackend(primary=primary, fallback=_FakeOllamaBackend())
        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True
        assert backend.active_backend == "ollama"

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_retryable_status_no_switch(self, status):
        """429/5xx 可重试 → 不降级，异常向上抛出（由底层 retry 处理）。"""
        primary = _FakePrimary(error=FakeAPIError(status))
        backend = FailoverLLMBackend(primary=primary, fallback=_FakeOllamaBackend())
        with pytest.raises(FakeAPIError):
            backend.chat("问题")
        assert backend.degraded is False

    def test_408_no_switch(self):
        """408（Request Timeout）为可重试 4xx 例外 → 不降级。"""
        primary = _FakePrimary(error=FakeAPIError(408))
        backend = FailoverLLMBackend(primary=primary, fallback=_FakeOllamaBackend())
        with pytest.raises(FakeAPIError):
            backend.chat("问题")
        assert backend.degraded is False

    def test_chat_with_tools_replays_same_args(self):
        """降级重放：备用后端收到与主后端相同的 messages/tools/tool_choice。"""
        primary = _FakePrimary(error=FakeAPIError(401))
        fallback = _FakeOllamaBackend()
        backend = FailoverLLMBackend(primary=primary, fallback=fallback)
        msgs = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "t", "description": "", "parameters": {}}}]
        resp = backend.chat_with_tools(msgs, tools, "auto")
        assert resp.content == "fallback-tools"
        assert backend.degraded is True
        assert fallback.tools_args == [(msgs, tools, "auto")]

    def test_primary_none_creation_degraded_tools(self):
        """创建期主后端缺失（如无 Key）→ 直接降级，chat_with_tools 走备用。"""
        backend = FailoverLLMBackend(primary=None, fallback=_FakeOllamaBackend())
        assert backend.degraded is True
        resp = backend.chat_with_tools([{"role": "user", "content": "hi"}], [])
        assert resp.content == "fallback-tools"


# ---------------------------------------------------------------------------
# 5. web_search 边界（REQ-UW1）
# ---------------------------------------------------------------------------

class TestWebSearchEdge:
    def _mock_client(self, available=True, results=None):
        client = MagicMock(spec=TavilySearchClient)
        client.is_available.return_value = available
        client.search.return_value = results or [
            {"title": "民事诉讼法修订", "url": "http://example.com/x", "content": "内容", "score": 0.9}
        ]
        return client

    def test_timeout_error_not_raised(self):
        """Tavily 超时 → ok=False + "搜索不可用"，不向调用方抛出。"""
        client = self._mock_client()
        client.search.side_effect = TimeoutError("timed out")
        tool = WebSearchTool(client)
        result = tool._exec(query="测试")
        assert not result.ok
        assert result.summary.startswith("搜索不可用")

    def test_unexpected_exception_not_raised(self):
        """Tavily 任意异常 → ok=False + "搜索不可用"（REQ-UW1 归一化）。"""
        client = self._mock_client()
        client.search.side_effect = RuntimeError("网络异常")
        tool = WebSearchTool(client)
        result = tool._exec(query="测试")
        assert not result.ok
        assert result.summary.startswith("搜索不可用")
        assert result.source == SOURCE_WEB

    def test_max_results_clamped_to_1_10(self):
        """max_results 越界 → 上限收敛到 10；0/负值语义：0=未指定→默认，负值→下限 1。"""
        client = self._mock_client()
        tool = WebSearchTool(client, default_max_results=5)
        tool._exec(query="q", max_results=999)
        assert client.search.call_args.kwargs["max_results"] == 10
        tool._exec(query="q", max_results=0)  # 0 = 未指定 → 默认 5
        assert client.search.call_args.kwargs["max_results"] == 5
        tool._exec(query="q", max_results=-3)  # 负值 → clamp 到 1
        assert client.search.call_args.kwargs["max_results"] == 1

    def test_tavily_search_raises_when_unavailable(self):
        """未配置 Key 时 TavilySearchClient.search 直接调用 → RuntimeError（由工具层归一化）。"""
        client = TavilySearchClient(api_key="")
        assert not client.is_available()
        with pytest.raises(RuntimeError, match="Tavily 未配置"):
            client.search("查询")

    def test_retrieve_knowledge_error_source_tag(self, fake_retriever):
        """内部检索失败 → ok=False + source=internal_kb + 首词"检索失败"（共享约定 §8.3）。"""
        from src.agents.tools.retrieve_knowledge import RetrieveKnowledgeTool
        fake_retriever.search = MagicMock(side_effect=RuntimeError("pg down"))
        tool = RetrieveKnowledgeTool(fake_retriever)
        result = tool._exec(query="测试")
        assert not result.ok
        assert result.source == SOURCE_INTERNAL_KB
        assert result.summary.startswith("检索失败")


# ---------------------------------------------------------------------------
# 6. graph 固定管线回退（AC-7 向后兼容）
# ---------------------------------------------------------------------------

class TestFixedPipelineFallback:
    def test_fixed_pipeline_stream_no_tool_events(self, monkeypatch):
        """AGENT_REACT_ENABLED=false → stream 不产出 tool_call/tool_result，旧事件流保留。"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", False)
        llm = FakeToolLLM([])
        agent = _build_agent(llm, react=False, monkeypatch=monkeypatch)
        assert agent._react_enabled is False
        assert agent._react is None
        events = list(agent.stream("行政拘留最长多久"))
        types = [e["type"] for e in events]
        assert "tool_call" not in types
        assert "tool_result" not in types
        assert "token" in types, "固定管线应正常产出 token 事件"
        assert "meta" in types, "固定管线应正常产出 meta 事件"

    def test_fixed_pipeline_ask_returns_answer(self, monkeypatch):
        """AGENT_REACT_ENABLED=false → ask() 走固定管线（含 retrieve 节点）并返回答案。"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", False)
        llm = FakeToolLLM([])
        agent = _build_agent(llm, react=False, monkeypatch=monkeypatch)
        graph_nodes = set(agent._graph.get_graph().nodes)
        assert "retrieve" in graph_nodes, "固定管线必须含 retrieve 节点（AC-7）"
        assert "agent" not in graph_nodes and "tools" not in graph_nodes
        result = agent.ask("行政拘留最长多久")
        assert result["answer"], "固定管线应能正常完成问答"
        assert len(result.get("retrieved_docs", [])) > 0, "固定管线自动执行内部检索"

    def test_react_disabled_even_with_tools_capable_llm(self, monkeypatch):
        """即使 LLM 具备工具调用能力，AGENT_REACT_ENABLED=false 仍强制固定管线。"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", False)
        llm = FakeToolLLM([_tool_call_response(), _final_response()])  # LLM 有 chat_with_tools
        agent = _build_agent(llm, react=False, monkeypatch=monkeypatch)
        assert agent._react_enabled is False
        # LLM 的 chat_with_tools 不会被调用（固定管线只用 chat/chat_stream）
        assert llm.calls == []
