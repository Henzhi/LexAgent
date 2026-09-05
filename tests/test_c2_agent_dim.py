"""C2 / M4 阶段 2 预埋：tool_log 与 SSE 工具事件的 agent 维度。

路线图（docs/M4-多Agent路线图.md §4 阶段 2 预埋项）：tool_log / SSE 事件加 agent
维度，让 SSE 能区分"谁在说话"。主 Agent 恒为 "main"，子 Agent 接入时传各自 id。
"""

from __future__ import annotations

from src.agents.state import AGENT_MAIN
from tests.fakes import FakeToolLLM
from tests.test_react_agent import _build_agent, _final_response, _tool_call_response


class TestToolLogAgentDim:
    def test_ask_tool_log_has_agent(self, monkeypatch):
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        llm = FakeToolLLM([_tool_call_response(), _final_response("答案")])
        agent = _build_agent(llm, monkeypatch)
        result = agent.ask("借款利息有什么规定", user_id="u1", session_id="s1")
        assert result["tool_log"], "应产生工具日志"
        assert result["tool_log"][0]["agent"] == AGENT_MAIN


class TestSSEAgentDim:
    def test_stream_tool_events_carry_agent(self, monkeypatch):
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        llm = FakeToolLLM([_tool_call_response(), _final_response("答案")])
        agent = _build_agent(llm, monkeypatch)
        events = list(agent.stream("借款利息有什么规定", history=[], user_id="u1", session_id="s1"))
        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        tool_results = [e for e in events if e.get("type") == "tool_result"]
        assert tool_calls and tool_results
        assert all(e["agent"] == AGENT_MAIN for e in tool_calls + tool_results)
