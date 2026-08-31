"""
M1 ReAct 循环测试：图结构、轮数上限、非法 tool_calls 容错、SSE 事件序列、固定管线回退。

不依赖外部服务：retriever 用 FakeRetriever，LLM 用 FakeToolLLM（可脚本化工具调用）。
"""

from __future__ import annotations

from tests.fakes import FakeRetriever, FakeToolLLM
from src.agents.graph import LawAgentGraph
from src.agents.tools import build_default_tools
from src.llm.base import ToolCall, ToolCallResponse


def _tool_call_response(query="测试") -> ToolCallResponse:
    """返回请求调用 retrieve_knowledge 的响应。"""
    return ToolCallResponse(
        content="",
        tool_calls=[ToolCall(id="call_1", name="retrieve_knowledge", arguments={"query": query})],
        raw={},
    )


def _final_response(text="根据《测试法》第一条，测试规定内容。") -> ToolCallResponse:
    return ToolCallResponse(content=text, tool_calls=[], raw={})


def _build_agent(llm, retriever=None, max_tool_turns=5, monkeypatch=None, react=True):
    """构造 LawAgentGraph（monkeypatch ReAct 开关/轮数上限）。"""
    if monkeypatch is not None:
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", react)
        monkeypatch.setattr("src.agents.graph.AGENT_MAX_TOOL_TURNS", max_tool_turns)
    retriever = retriever or FakeRetriever()
    registry = build_default_tools(retriever)
    agent = LawAgentGraph(
        retriever=retriever,
        llm=llm,
        top_k=3,
        max_retries=0,
        memory_manager=None,
        faq_cache=None,
        query_logger=None,
        registry=registry,
    )
    return agent


# ---------------------------------------------------------------------------
# 图结构与模式选择
# ---------------------------------------------------------------------------


class TestGraphMode:
    def test_react_mode_selected_by_default(self, monkeypatch):
        """AGENT_REACT_ENABLED=true → ReAct 图（agent/tools 节点存在）。"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        agent = _build_agent(FakeToolLLM([_final_response()]), monkeypatch=monkeypatch)
        assert agent._react_enabled is True
        assert agent._react is not None
        graph_nodes = set(agent._graph.get_graph().nodes)
        assert {"intent", "memory_retrieve", "agent", "tools", "validate"} <= graph_nodes

    def test_fixed_mode_when_disabled(self, monkeypatch):
        """AGENT_REACT_ENABLED=false → 固定管线图（无 agent/tools 节点）。"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", False)
        agent = _build_agent(FakeToolLLM([_final_response()]), monkeypatch=monkeypatch, react=False)
        assert agent._react_enabled is False
        assert agent._react is None
        graph_nodes = set(agent._graph.get_graph().nodes)
        assert "agent" not in graph_nodes
        assert "tools" not in graph_nodes
        assert "retrieve" in graph_nodes


# ---------------------------------------------------------------------------
# ask() 同步路径
# ---------------------------------------------------------------------------


class TestAskReact:
    def test_react_loop_with_tool_then_answer(self, monkeypatch):
        """LLM 先调工具再给答案 → ask() 返回答案 + tool_log。"""
        llm = FakeToolLLM([_tool_call_response(), _final_response("最终答案文本")])
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert result["answer"] == "最终答案文本"
        assert len(result["tool_log"]) == 1
        assert result["tool_log"][0]["tool"] == "retrieve_knowledge"
        assert result["tool_log"][0]["ok"] is True
        # LLM 第一次收到工具 schema，第二次（决策最终答案）收到工具回灌消息
        assert llm.calls[0]["tools"]  # 第一轮有工具
        tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert tool_messages, "第二轮应包含 tool 回灌消息"

    def test_direct_answer_without_tools(self, monkeypatch):
        """LLM 直接给出最终答案（不调用工具）→ 不产出 tool_log。"""
        llm = FakeToolLLM([_final_response("直接回答")])
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert result["answer"] == "直接回答"
        assert result["tool_log"] == []

    def test_max_tool_turns_forces_answer(self, monkeypatch):
        """LLM 每轮都要求调用工具 → 达到轮数上限后强制产出答案（REQ-UW4）。"""
        monkeypatch.setattr("src.agents.graph.AGENT_MAX_TOOL_TURNS", 3)
        # 脚本永远返回工具调用（无法自然收敛）
        llm = FakeToolLLM([_tool_call_response()] * 10)
        agent = _build_agent(llm, max_tool_turns=3, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert result["answer"], "达到上限后必须产出答案"
        # agent_turns 不超过 上限+1（强制作答轮）
        assert result["agent_turns"] <= 4

    def test_max_turns_strips_dsml_from_answer(self, monkeypatch):
        """达到轮数上限后 LLM 在纯文本输出 DSML 工具调用语法 → 兜底清除（自然语言答案）。"""
        from src.agents.react_nodes import _strip_dsml_tool_calls

        # 单元：纯 DSML 块被清除
        assert (
            _strip_dsml_tool_calls(
                '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="x"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
            )
            == ""
        )
        # 单元：DSML 块外的自然语言保留
        mixed = "前面文字<｜｜DSML｜｜tool_calls>xxx</｜｜DSML｜｜tool_calls>后面文字"
        assert _strip_dsml_tool_calls(mixed) == "前面文字后面文字"
        # 无 DSML 不动
        assert _strip_dsml_tool_calls("普通答案") == "普通答案"

        # 集成：上限轮返回 DSML 文本 → answer 不含 DSML
        monkeypatch.setattr("src.agents.graph.AGENT_MAX_TOOL_TURNS", 2)
        dsml_resp = ToolCallResponse(
            content='我来检索<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="retrieve_knowledge"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>',
            tool_calls=[],
            raw={},
        )
        llm = FakeToolLLM([_tool_call_response(), _tool_call_response(), dsml_resp])
        agent = _build_agent(llm, max_tool_turns=2, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert "DSML" not in result["answer"]
        assert result["answer"], "清除后仍应有可用答案（兜底文案）"

    def test_invalid_tool_arguments_fed_back(self, monkeypatch):
        """工具参数不合法 → 不执行工具，回灌错误消息（R1 容错）。

        D-M3-13 前测的是「arguments JSON 字符串非法」（parse_error）；
        迁移到 LangChain 后参数在框架层就已解析为 dict，非法 JSON 的情况
        不再存在（Pydantic 会先拦截）。因此改为测更贴近现实的场景：
        模型传了缺少必填字段的参数 —— 容错机制本身不变。
        """
        llm = FakeToolLLM(
            [
                ToolCallResponse(
                    content="",
                    # 缺 query（必填）→ 工具执行应失败并回灌错误
                    tool_calls=[ToolCall(id="bad_1", name="retrieve_knowledge", arguments={})],
                    raw={},
                ),
                _final_response("容错后回答"),
            ]
        )
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert result["answer"] == "容错后回答"
        assert len(result["tool_log"]) == 1
        assert result["tool_log"][0]["ok"] is False
        # 错误信息回灌给模型，循环能继续并产出答案
        assert "参数" in result["tool_log"][0]["summary"]
        # 第二轮 LLM 应看到工具错误消息
        tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert tool_msgs and "参数" in tool_msgs[0]["content"]

    def test_unknown_tool_returns_error_result(self, monkeypatch):
        """LLM 请求未知工具 → 工具返回 ok=False 的 ToolResult，循环继续。"""
        llm = FakeToolLLM(
            [
                ToolCallResponse(content="", tool_calls=[ToolCall(id="x1", name="not_exist", arguments={})], raw={}),
                _final_response("未知工具容错回答"),
            ]
        )
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert result["answer"] == "未知工具容错回答"
        assert result["tool_log"][0]["ok"] is False
        assert "未知工具" in result["tool_log"][0]["summary"]

    def test_empty_name_tool_call_routes_to_final_answer(self, monkeypatch):
        """空 name 的 tool_call（DeepSeek V4 空占位）→ 不路由 tools，直接最终答案。"""
        llm = FakeToolLLM(
            [
                ToolCallResponse(
                    content="直接回答",
                    tool_calls=[ToolCall(id="c1", name="", arguments={})],
                    raw={},
                ),
            ]
        )
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        result = agent.ask("行政拘留最长多久")
        assert result["answer"] == "直接回答"
        # 空 name 占位被过滤，不会进入 tools 节点产生"未知工具"日志
        assert result["tool_log"] == []
        assert result["agent_turns"] == 1


# ---------------------------------------------------------------------------
# stream() 流式路径（SSE 事件序列）
# ---------------------------------------------------------------------------


class TestStreamReact:
    def test_emits_tool_events_and_tokens(self, monkeypatch):
        """SSE 事件序列：tool_call → tool_result → meta → token（F4/AC-5）。"""
        llm = FakeToolLLM([_tool_call_response(), _final_response("根据《测试法》第一条，这是最终答案。")])
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        events = list(agent.stream("行政拘留最长多久"))
        types = [e["type"] for e in events]

        assert "tool_call" in types
        assert "tool_result" in types
        assert "token" in types
        assert "meta" in types

        tool_call_event = next(e for e in events if e["type"] == "tool_call")
        assert tool_call_event["tool"] == "retrieve_knowledge"
        assert tool_call_event["turn"] >= 1

        tool_result_event = next(e for e in events if e["type"] == "tool_result")
        assert tool_result_event["ok"] is True
        assert "检索到 1 条相关法条" in tool_result_event["summary"]
        assert len(tool_result_event["summary"]) <= 300

        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        assert "最终答案" in token_text

    def test_tool_result_failure_marked(self, monkeypatch):
        """web_search 不可用 → tool_result.ok=false + summary 首词"搜索不可用"（REQ-UW1/AC-2）。"""
        llm = FakeToolLLM(
            [
                ToolCallResponse(
                    content="",
                    tool_calls=[ToolCall(id="w1", name="web_search", arguments={"query": "最新修订"})],
                    raw={},
                ),
                _final_response("基于内部库回答"),
            ]
        )
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        events = list(agent.stream("行政拘留最长多久"))
        tool_result_event = next(e for e in events if e["type"] == "tool_result")
        assert tool_result_event["ok"] is False
        assert tool_result_event["summary"].startswith("搜索不可用")

    def test_casual_query_bypasses_react(self, monkeypatch):
        """闲聊意图 → 不进入 ReAct 循环，直接回复（与固定管线一致）。"""
        llm = FakeToolLLM([])
        agent = _build_agent(llm, monkeypatch=monkeypatch)
        events = list(agent.stream("你好呀", history=[]))
        types = [e["type"] for e in events]
        assert "tool_call" not in types
        assert "token" in types


# ---------------------------------------------------------------------------
# agent_node 决策调用的入口语义（D-M1-3 重试 + Failover 4xx 降级回归守卫）
# ---------------------------------------------------------------------------


class TestAgentNodeCallSemantics:
    """agent_node 必须经 `chat_with_tools` 公开入口调用 LLM。

    D-M3-13 迁移期间曾改为直接 `chat_model.bind_tools().invoke()`，同时绕过了
    该入口链路上的两层语义：D-M1-3 重试（429/5xx 不再重试）与 FailoverLLMBackend
    的 4xx 运行期降级（主后端 4xx 不再切 Ollama，ReAct 循环直接给出失败答案）。
    本组测试用「只会抛错 / 只在 _chat_with_tools_impl 里应答」的假后端守住回归——
    若 agent_node 再走 chat_model 裸调用，FakePrimaryBackend 的 chat_model 未初始化
    会直接 NotImplementedError，测试必然失败。
    """

    def _react(self, backend):
        from src.agents.react_nodes import make_react_nodes

        return make_react_nodes(backend, build_default_tools(FakeRetriever()))

    def _state(self):
        return {
            "query": "测试问题",
            "messages": [],
            "agent_turns": 0,
            "tool_calls": [],
            "tool_results": [],
            "tool_log": [],
        }

    def test_primary_4xx_degrades_to_fallback(self):
        from src.llm.failover import FailoverLLMBackend
        from tests.test_failover import FakeAPIError, FakeOllamaBackend, FakePrimaryBackend

        primary = FakePrimaryBackend(error=FakeAPIError(401))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        update = self._react(backend)["agent"](self._state())
        # 4xx → 降级 Ollama 并用备用后端的结果作答（而非"模型调用失败"兜底文案）
        assert update["answer"] == "fallback-tools"
        assert backend.degraded is True

    def test_primary_429_no_degradation_failure_answer(self):
        from src.llm.failover import FailoverLLMBackend
        from tests.test_failover import FakeAPIError, FakeOllamaBackend, FakePrimaryBackend

        primary = FakePrimaryBackend(error=FakeAPIError(429))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        update = self._react(backend)["agent"](self._state())
        # 429 属可重试故障 → 不降级，agent 节点按调用失败兜底
        assert update["answer"] == "抱歉，模型调用失败，暂时无法回答该问题。"
        assert backend.degraded is False

    def test_answer_flows_through_public_entry(self):
        """正常路径：经 chat_with_tools 拿到最终答案（FakeToolLLM 记录调用参数）。"""
        llm = FakeToolLLM([_final_response("公开入口回答")])
        update = self._react(llm)["agent"](self._state())
        assert update["answer"] == "公开入口回答"
        assert llm.calls, "agent_node 应通过 chat_with_tools 公开入口调用"
        assert llm.calls[0]["tools"], "第一轮应携带工具 schema"


# ---------------------------------------------------------------------------
# 强制作答轮"过渡语"兜底（2026-08-31 体验联调发现：最终答案是一句检索计划）
# ---------------------------------------------------------------------------


class TestForcedAnswerTransitionGuard:
    """达到轮数上限被强制作答时，模型可能输出"让我进一步检索……"式过渡语。

    该过渡语会被当成最终答案推给用户（审核器只查幻觉、不查"是否回答了问题"）。
    修复：强制作答轮检测到过渡语 → 追加明确指令重试一次，取更完整的回答。
    """

    def _react(self, backend, max_turns=2):
        from src.agents.react_nodes import make_react_nodes

        return make_react_nodes(backend, build_default_tools(FakeRetriever()), max_tool_turns=max_turns)

    def _state(self, turns):
        return {
            "query": "测试问题",
            "messages": [],
            "agent_turns": turns,
            "tool_calls": [],
            "tool_results": [],
            "tool_log": [],
        }

    def test_transition_answer_retried(self):
        """强制作答轮输出过渡语 → 重试一次并采纳更完整的回答。"""
        llm = FakeToolLLM(
            [
                ToolCallResponse(content="让我进一步检索相关司法解释以便全面回答您的问题。", tool_calls=[]),
                _final_response("根据《刑法》第二十条，正当防卫不负刑事责任（来源：内部知识库）。"),
            ]
        )
        update = self._react(llm)["agent"](self._state(2))  # turns=2 >= max_turns=2
        assert "正当防卫" in update["answer"]
        assert "让我" not in update["answer"]
        assert len(llm.calls) == 2, "应触发一次重试"

    def test_retry_result_not_adopted_when_shorter(self):
        """重试输出比原过渡语更短时，不采纳（保留原输出）。"""
        llm = FakeToolLLM(
            [
                ToolCallResponse(content="让我进一步检索相关司法解释以便全面回答您的问题。", tool_calls=[]),
                ToolCallResponse(content="好的。", tool_calls=[]),
            ]
        )
        update = self._react(llm)["agent"](self._state(2))
        assert update["answer"].startswith("让我")

    def test_normal_round_transition_not_retried(self):
        """正常轮的过渡语 content 随 tool_calls 回灌，不触发重试。"""
        llm = FakeToolLLM(
            [
                ToolCallResponse(
                    content="让我检索相关法条。",
                    tool_calls=[ToolCall(id="c1", name="retrieve_knowledge", arguments={"query": "正当防卫"})],
                ),
            ]
        )
        update = self._react(llm)["agent"](self._state(0))
        assert update.get("answer") is None
        assert update["tool_calls"], "正常轮应路由去 tools"
        assert len(llm.calls) == 1, "不应触发重试"

    def test_transition_detector(self):
        from src.agents.react_nodes import _looks_like_transition

        assert _looks_like_transition("让我进一步检索相关司法解释以便全面回答您的问题。")
        assert _looks_like_transition("我将查询更多资料")
        assert not _looks_like_transition("根据《刑法》第二十条，正当防卫不负刑事责任。")
