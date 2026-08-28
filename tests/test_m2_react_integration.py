"""
M2 ReAct 集成测试：三路证据累计（tools_node）、融合 sources 进入 SSE meta 与 ask() 结果。

不依赖外部服务：retriever 用 FakeRetriever，Tavily 用 MagicMock，LLM 用 FakeToolLLM。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.graph import LawAgentGraph
from src.agents.tools import build_default_tools
from src.llm.base import ToolCall, ToolCallResponse
from src.search.fusion import VERIFIED_INTERNAL, WEB_UNVERIFIED
from tests.fakes import FakeRetriever, FakeToolLLM


def _fake_tavily():
    """可用假 Tavily：返回提及《测试法》的网络线索（制造与内部库的冲突场景）。"""
    tavily = MagicMock()
    tavily.is_available.return_value = True
    tavily.search.return_value = [
        {"title": "《测试法》最新修订解读", "url": "https://x.com/1",
         "content": "《测试法》2026 年发布了修订内容", "score": 0.9},
    ]
    return tavily


def _build_agent(llm, tavily=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        monkeypatch.setattr("src.agents.graph.AGENT_MAX_TOOL_TURNS", 5)
    retriever = FakeRetriever()
    registry = build_default_tools(retriever, tavily_client=tavily)
    return LawAgentGraph(
        retriever=retriever, llm=llm,
        top_k=3, max_retries=0,
        memory_manager=None, faq_cache=None, query_logger=None,
        registry=registry,
    )


def _parallel_tools_response() -> ToolCallResponse:
    """一轮同时请求内部检索 + 网络搜索（DeepSeek parallel_tool_calls，F6 双路并行）。"""
    return ToolCallResponse(
        content="",
        tool_calls=[
            ToolCall(id="c1", name="retrieve_knowledge", arguments={"query": "测试"}),
            ToolCall(id="c2", name="web_search", arguments={"query": "测试法 最新修订"}),
        ],
        raw={},
    )


def _final_response(text="根据《测试法》第一条，测试规定内容。") -> ToolCallResponse:
    return ToolCallResponse(content=text, tool_calls=[], raw={})


class TestDualRouteEvidence:
    def test_ask_accumulates_and_fuses_dual_evidence(self, monkeypatch):
        """并行调用内部检索+网络搜索 → fused_sources 含两路证据与验证状态（F6/F7/F10）。"""
        llm = FakeToolLLM([_parallel_tools_response(), _final_response("融合后的答案")])
        agent = _build_agent(llm, tavily=_fake_tavily(), monkeypatch=monkeypatch)
        result = agent.ask("测试法怎么规定")

        fused = result.get("fused_sources") or []
        sources = {s["source"] for s in fused}
        assert "internal_kb" in sources
        assert "web" in sources
        verifications = {s["verification"] for s in fused}
        assert VERIFIED_INTERNAL in verifications
        assert WEB_UNVERIFIED in verifications

    def test_conflict_reported_in_ask(self, monkeypatch):
        """web 线索提及内部库法名《测试法》→ conflict_laws 非空（REQ-UW3）。"""
        llm = FakeToolLLM([_parallel_tools_response(), _final_response()])
        agent = _build_agent(llm, tavily=_fake_tavily(), monkeypatch=monkeypatch)
        result = agent.ask("测试法怎么规定")
        assert "测试法" in result.get("conflict_laws", [])

    def test_stream_meta_carries_verification(self, monkeypatch):
        """SSE meta.sources 每条携带 verification；冲突时推送裁决提示事件（F8/F10）。"""
        llm = FakeToolLLM([_parallel_tools_response(), _final_response("最终答案")])
        agent = _build_agent(llm, tavily=_fake_tavily(), monkeypatch=monkeypatch)
        events = list(agent.stream("测试法怎么规定"))

        meta = next(e for e in events if e["type"] == "meta")
        assert all("verification" in s for s in meta["sources"])
        # 冲突裁决提示（REQ-UW3：告知用户以内部库为准）
        thinking = [e["content"] for e in events if e["type"] == "thinking"]
        assert any("内部库优先" in t for t in thinking)

    def test_web_only_without_conflict_no_warning(self, monkeypatch):
        """无冲突时不推送裁决提示。"""
        tavily = MagicMock()
        tavily.is_available.return_value = True
        tavily.search.return_value = [
            {"title": "无关新闻", "url": "https://x.com/9", "content": "无关内容", "score": 0.7},
        ]
        llm = FakeToolLLM([
            ToolCallResponse(
                content="", raw={},
                tool_calls=[ToolCall(id="c2", name="web_search", arguments={"query": "随便"})],
            ),
            _final_response("仅网络线索的答案"),
        ])
        agent = _build_agent(llm, tavily=tavily, monkeypatch=monkeypatch)
        events = list(agent.stream("测试法怎么规定"))
        thinking = [e["content"] for e in events if e["type"] == "thinking"]
        assert not any("内部库优先" in t for t in thinking)

    def test_web_results_accumulate_across_turns(self, monkeypatch):
        """跨轮多次 web_search → web_results 按 URL 去重累计（F7）。"""
        tavily = MagicMock()
        tavily.is_available.return_value = True
        tavily.search.return_value = [
            {"title": "线索A", "url": "https://x.com/a", "content": "c", "score": 0.8},
            {"title": "线索B", "url": "https://x.com/b", "content": "c", "score": 0.7},
        ]
        script = [
            ToolCallResponse(content="", raw={},
                             tool_calls=[ToolCall(id="w1", name="web_search", arguments={"query": "q1"})]),
            ToolCallResponse(content="", raw={},
                             tool_calls=[ToolCall(id="w2", name="web_search", arguments={"query": "q2"})]),
            _final_response(),
        ]
        llm = FakeToolLLM(script)
        agent = _build_agent(llm, tavily=tavily, monkeypatch=monkeypatch)
        result = agent.ask("测试法怎么规定")
        # 两轮同 URL 列表 → 去重后仅 2 条 web 证据
        web_sources = [s for s in result.get("fused_sources", []) if s["source"] == "web"]
        assert len(web_sources) == 2


class TestStreamMessageHistoryIntegrity:
    """回归：流式路径 ReAct 第 2 轮消息序列完整性（DeepSeek 400 修复）。

    Bug：_stream_react 用 state.update() 整体替换 messages，第 2 轮
    chat_with_tools 的消息里 tool 消息前没有 assistant(tool_calls)，
    DeepSeek 返回 400 并降级 Ollama。
    """

    def test_second_turn_messages_keep_assistant_tool_calls(self, monkeypatch):
        llm = FakeToolLLM([_parallel_tools_response(), _final_response("流式最终答案")])
        agent = _build_agent(llm, tavily=_fake_tavily(), monkeypatch=monkeypatch)
        events = list(agent.stream("测试法怎么规定"))
        assert any(e["type"] == "tool_call" for e in events)

        # 第 2 次 chat_with_tools 的消息序列必须满足 OpenAI 约定：
        # 每个 tool 消息块的前面是带 tool_calls 的 assistant 消息，
        # 且 tool_call_id 能对应上（并行 tool 调用时 tool 消息可连续排列）
        assert len(llm.calls) >= 2
        msgs = llm.calls[1]["messages"]
        roles = [m.get("role") for m in msgs]
        assert "tool" in roles, f"第 2 轮应包含 tool 消息: {roles}"
        # 找到带 tool_calls 的 assistant 消息，收集其全部 id
        assistant_call_ids: set[str] = set()
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assistant_call_ids.update(tc.get("id") for tc in m["tool_calls"])
        assert assistant_call_ids, f"第 2 轮应包含带 tool_calls 的 assistant 消息: {roles}"
        # tool 消息必须出现在 assistant(tool_calls) 之后，且 id 匹配
        first_assistant_idx = next(
            i for i, m in enumerate(msgs)
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        for i, m in enumerate(msgs):
            if m.get("role") == "tool":
                assert i > first_assistant_idx, "tool 消息必须在 assistant(tool_calls) 之后"
                assert m.get("tool_call_id") in assistant_call_ids, (
                    f"tool_call_id 不匹配: {m.get('tool_call_id')} 不在 {assistant_call_ids}"
                )

    def test_second_turn_keeps_user_query(self, monkeypatch):
        """第 2 轮消息序列仍包含原始 user 问题（不被覆盖丢失）。"""
        llm = FakeToolLLM([_parallel_tools_response(), _final_response("答案")])
        agent = _build_agent(llm, tavily=_fake_tavily(), monkeypatch=monkeypatch)
        list(agent.stream("测试法怎么规定"))
        msgs = llm.calls[1]["messages"]
        assert any(m.get("role") == "user" and "测试法" in str(m.get("content", "")) for m in msgs)
