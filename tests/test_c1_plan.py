"""C1 / M4 阶段 1（D-M4-1）：plan 对象 + 场景工具白名单。

退出判据（docs/M4-多Agent路线图.md §4 阶段 1）：
- A 类行为与现状逐字一致（回归守卫：A 类 plan.tools 为空 → 全量 schema 不变）；
- B 类确认单携带场景工具白名单（SCENES.tools 首次被消费）；
- agent_node 按 plan 白名单过滤 schemas（交集为空 fail-open）。
"""

from __future__ import annotations


from src.agents.graph import LawAgentGraph
from src.agents.react_nodes import make_react_nodes
from src.agents.state import AgentState
from src.agents.tools import build_default_tools
from src.llm.base import ToolCallResponse
from src.memory.confirmation_store import ConfirmationStore
from src.rag.scenes import KIND_A, KIND_B, build_plan, classify_scene
from tests.fakes import FakeRetriever, FakeToolLLM

B_QUERY = "帮我起草一份房屋租赁合同"  # → contract_draft（B 类）
A_QUERY = "行政拘留最长多久"  # → A 类
_NOOP_ANSWER = ToolCallResponse(content="这是最终答案。", tool_calls=[], raw={})


def _schema_names(tools: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _make_graph(llm) -> LawAgentGraph:
    retriever = FakeRetriever()
    return LawAgentGraph(
        retriever=retriever,
        llm=llm,
        top_k=3,
        max_retries=0,
        memory_manager=None,
        faq_cache=None,
        query_logger=None,
        registry=build_default_tools(retriever),
        confirmation_store=ConfirmationStore(redis_url=""),
    )


# ---------------------------------------------------------------------------
# build_plan：SceneMatch → AgentPlan
# ---------------------------------------------------------------------------


class TestBuildPlan:
    def test_b_scene_carries_whitelist(self):
        plan = build_plan(classify_scene(B_QUERY))
        assert plan.kind == KIND_B
        assert plan.needs_confirmation is True
        assert plan.restricts_tools() is True
        assert "retrieve_knowledge" in plan.tools

    def test_a_scene_keeps_full_toolset(self):
        """A 类回归守卫的 plan 侧依据：白名单置空 = 不收窄。"""
        plan = build_plan(classify_scene(A_QUERY))
        assert plan.kind == KIND_A
        assert plan.tools == ()
        assert plan.needs_confirmation is False
        assert plan.restricts_tools() is False

    def test_fallback_plan_no_restriction(self):
        plan = build_plan(classify_scene("asdkjhqwe"))  # 未命中 → 保守回落
        assert plan.matched is False
        assert plan.kind == KIND_A
        assert plan.tools == ()


# ---------------------------------------------------------------------------
# agent_node：按 plan 白名单过滤 schemas
# ---------------------------------------------------------------------------


class TestAgentNodePlanFilter:
    def _run(self, plan: dict | None) -> list[str]:
        llm = FakeToolLLM(script=[_NOOP_ANSWER])
        nodes = make_react_nodes(llm, build_default_tools(FakeRetriever()), max_tool_turns=3)
        state: AgentState = {
            "query": A_QUERY,
            "messages": [],
            "agent_turns": 0,
        }
        if plan is not None:
            state["plan"] = plan
        nodes["agent"](state)
        return _schema_names(llm.calls[0]["tools"])

    def test_b_plan_filters_schemas(self):
        full = self._run({})
        plan = {"scene_id": "contract_draft", "tools": ["retrieve_knowledge", "web_search"]}
        got = self._run(plan)
        assert got == ["retrieve_knowledge", "web_search"]
        assert len(got) < len(full)

    def test_a_plan_full_schemas(self):
        """A 类回归守卫：plan.tools 为空 → schema 与无 plan 时完全一致。"""
        assert self._run({}) == self._run({"scene_id": "legal_qa", "tools": []})

    def test_no_plan_backward_compat(self):
        assert self._run(None) == self._run({})

    def test_unknown_whitelist_fails_open(self):
        """白名单与注册表无交集 → fail-open 不收窄（场景清单脱钩不阻断回答）。"""
        full = self._run({})
        got = self._run({"tools": ["not_a_tool"]})
        assert got == full


# ---------------------------------------------------------------------------
# F12 确认单携带白名单 + ask() 双路径落 plan
# ---------------------------------------------------------------------------


class TestConfirmationCarriesTools:
    def test_pending_confirmation_payload_has_tools(self):
        agent = _make_graph(FakeToolLLM())
        plan = build_plan(classify_scene(B_QUERY))
        payload = agent._pending_confirmation("u1", "s1", B_QUERY, plan)
        assert payload is not None
        assert payload["tools"] == list(plan.tools)
        assert payload["tools"]  # B 类白名单非空

    def test_ask_b_query_payload_contains_tools(self):
        agent = _make_graph(FakeToolLLM())
        result = agent.ask(B_QUERY, user_id="u1", session_id="s1")
        payload = result.get("confirmation_required")
        assert payload, "B 类未确认应返回确认载荷"
        assert payload["scene"] == "contract_draft"
        assert "retrieve_knowledge" in payload["tools"]


class TestAskPathRegressionGuard:
    def test_a_query_full_schemas_end_to_end(self):
        """A 类端到端回归守卫：进图 schema 必须仍是全量工具表（逐字一致）。"""
        llm = FakeToolLLM(script=[_NOOP_ANSWER])
        agent = _make_graph(llm)
        result = agent.ask(A_QUERY, user_id="u1", session_id="s1")
        assert result["answer"]
        full_names = _schema_names(build_default_tools(FakeRetriever()).to_openai_schemas())
        assert _schema_names(llm.calls[0]["tools"]) == full_names
