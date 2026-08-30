"""场景分类单元测试（M3 / F11）— src/rag/scenes.py

覆盖：PRD §5.2 的 10 个场景识别、A/B 类归属、未命中保守回落、
关键词冲突回归（合同起草 vs 合同审查 vs 法律检索）、配置完整性。
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeRetriever, FakeToolLLM
from src.agents.graph import LawAgentGraph
from src.agents.tools import build_default_tools
from src.llm.base import ToolCallResponse
from src.rag.scenes import (
    KIND_A,
    KIND_B,
    DEFAULT_SCENE,
    SCENES,
    SceneMatch,
    classify_scene,
    get_scene,
    scene_ids,
)


# ---------------------------------------------------------------------------
# 配置完整性（防止改场景清单时改坏）
# ---------------------------------------------------------------------------


def test_scene_ids_unique():
    """场景 id 必须唯一（scene_id 会写入 state，重复会导致分类结果不可信）"""
    ids = scene_ids()
    assert len(ids) == len(set(ids)), f"场景 id 存在重复: {ids}"


def test_all_scenes_have_valid_kind():
    """kind 只能是 A 或 B"""
    for scene in SCENES:
        assert scene.kind in (KIND_A, KIND_B), f"{scene.id} 的 kind 非法: {scene.kind}"


def test_scenes_have_match_rules():
    """每个场景至少有一种匹配规则，否则永远匹配不到（配置遗漏）"""
    for scene in SCENES:
        has_rule = bool(scene.keywords or scene.strong_keywords or scene.patterns)
        assert has_rule, f"{scene.id} 未配置任何匹配规则"


def test_prd_ten_scenes_present():
    """PRD §5.2 定义的 10 个典型场景全部在清单中"""
    expected = {
        "legal_lookup",
        "legal_qa",
        "regulation_tracking",
        "case_analysis",
        "similar_case_report",
        "contract_draft",
        "contract_review",
        "legal_document",
        "due_diligence",
        "compliance_check",
    }
    assert expected.issubset(set(scene_ids())), f"缺失场景: {expected - set(scene_ids())}"


def test_default_scene_is_class_a():
    """默认回落场景必须是 A 类 —— 保守回落不应把用户拖进确认流程"""
    assert DEFAULT_SCENE.kind == KIND_A
    assert DEFAULT_SCENE.id == "legal_qa"


def test_patterns_compile():
    """所有正则都能编译（__post_init__ 预编译，非法正则会在导入期炸）"""
    for scene in SCENES:
        assert len(scene._compiled) == len(scene.patterns)


# ---------------------------------------------------------------------------
# 场景识别（PRD §5.2 的 10 个场景）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected_id,expected_kind",
    [
        # ---- A 类：全自动 ----
        ("劳动合同法第四十六条是什么", "legal_lookup", KIND_A),
        ("民法典关于违约金是怎么规定的", "legal_lookup", KIND_A),
        ("刑法第二百三十四条的内容", "legal_lookup", KIND_A),
        ("治安管理处罚法有没有关于寻衅滋事的规定", "legal_lookup", KIND_A),
        ("打架被拘留最长多久，违法吗", "legal_qa", KIND_A),
        ("公司辞退我需要赔偿吗", "legal_qa", KIND_A),
        ("2026年劳动法有没有新规定出台", "regulation_tracking", KIND_A),
        ("最近有没有关于个人信息保护的新规", "regulation_tracking", KIND_A),
        ("帮我分析一下这个判决案例", "case_analysis", KIND_A),
        ("这个案子法院会怎么判", "case_analysis", KIND_A),
        # ---- B 类：需人工确认 ----
        ("帮我做一份类案检索报告", "similar_case_report", KIND_B),
        ("帮我检索一下类似的判决案例并出报告", "similar_case_report", KIND_B),
        ("帮我起草一份房屋租赁合同", "contract_draft", KIND_B),
        ("拟一份股权转让协议", "contract_draft", KIND_B),
        ("帮我审查一下这份劳动合同有没有问题", "contract_review", KIND_B),
        ("这份合同的风险点帮我审核一下", "contract_review", KIND_B),
        ("帮我写一份起诉状", "legal_document", KIND_B),
        ("起草一份劳动仲裁申请书", "legal_document", KIND_B),
        ("帮我做一下这家公司的尽调", "due_diligence", KIND_B),
        ("投前帮我做一次尽职调查", "due_diligence", KIND_B),
        ("帮我做一次合规检查", "compliance_check", KIND_B),
        ("这份证据材料帮我做个证据分类", "compliance_check", KIND_B),
    ],
)
def test_classify_scene(query, expected_id, expected_kind):
    match = classify_scene(query)
    assert match.scene_id == expected_id, f"{query!r} 期望 {expected_id}，实际 {match.scene_id}"
    assert match.kind == expected_kind
    assert match.matched is True
    assert match.score > 0


# ---------------------------------------------------------------------------
# A/B 类归属（F12 的判据，错了会弹错确认）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "劳动合同法第四十六条是什么",
        "打架被拘留违法吗",
        "2026年劳动法有没有新规",
        "帮我分析一下这个案例",
    ],
)
def test_a_class_never_needs_confirmation(query):
    """A 类场景不应触发确认"""
    match = classify_scene(query)
    assert match.kind == KIND_A
    assert match.needs_confirmation() is False


@pytest.mark.parametrize(
    "query",
    [
        "帮我起草一份房屋租赁合同",
        "帮我审查这份合同",
        "帮我写一份起诉状",
        "帮我做一次尽调",
        "帮我做一次合规检查",
        "帮我做一份类案检索报告",
    ],
)
def test_b_class_needs_confirmation(query):
    """B 类场景必须触发确认"""
    match = classify_scene(query)
    assert match.kind == KIND_B
    assert match.needs_confirmation() is True


# ---------------------------------------------------------------------------
# 关键词冲突回归（最易回归的地方）
# ---------------------------------------------------------------------------


def test_contract_draft_vs_review_not_confused():
    """「合同」同时命中起草与审查，靠强特征词区分，不能混淆"""
    assert classify_scene("帮我起草一份房屋租赁合同").scene_id == "contract_draft"
    assert classify_scene("帮我审查一下这份房屋租赁合同").scene_id == "contract_review"


def test_law_article_query_not_mistaken_for_contract_scene():
    """「劳动合同法第四十六条」含「合同」二字，但不能被判成合同类 B 类场景。

    这条是最关键的回归：若被误判为 B 类，用户查个法条也要先确认，体验灾难。
    正则（第X条）权重 3.0 + 强特征词 2.0 必须压过「合同」的 1.0。
    """
    match = classify_scene("劳动合同法第四十六条是什么")
    assert match.scene_id == "legal_lookup"
    assert match.kind == KIND_A, "查法条被误判为需确认场景"


def test_contract_dispute_qa_not_mistaken_for_draft():
    """「合同违约怎么赔偿」是咨询不是起草"""
    match = classify_scene("合同违约怎么赔偿，违法吗")
    assert match.scene_id != "contract_draft"


# ---------------------------------------------------------------------------
# 未命中回落（REQ-UW：不因分类失败阻断回答）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "今天天气怎么样",
        "啊",
        "？",
        "asdfghjkl",
    ],
)
def test_unmatched_falls_back_to_default_a_class(query):
    """未命中任何场景 → 回落默认 A 类场景，matched=False"""
    match = classify_scene(query)
    assert match.scene_id == DEFAULT_SCENE.id
    assert match.kind == KIND_A
    assert match.matched is False
    assert match.score == 0.0
    assert match.needs_confirmation() is False


@pytest.mark.parametrize("query", ["", "   ", None])
def test_empty_input_is_safe(query):
    """空输入不抛异常，回落到默认场景"""
    match = classify_scene(query)
    assert isinstance(match, SceneMatch)
    assert match.scene_id == DEFAULT_SCENE.id
    assert match.kind == KIND_A
    assert match.matched is False


# ---------------------------------------------------------------------------
# 辅助接口
# ---------------------------------------------------------------------------


def test_get_scene():
    assert get_scene("contract_draft") is not None
    assert get_scene("contract_draft").name == "合同起草"
    assert get_scene("nonexistent_scene") is None


def test_classify_scene_tools_match_config():
    """分类结果带回的 tools 必须与场景配置一致（供后续工具白名单用）"""
    match = classify_scene("帮我起草一份房屋租赁合同")
    assert match.tools == get_scene("contract_draft").tools
    assert "retrieve_knowledge" in match.tools


# ---------------------------------------------------------------------------
# 与 graph 的集成（分类结果是否真的进入了两条执行路径）
# ---------------------------------------------------------------------------


def _final_response(text="根据《测试法》第一条，测试规定内容。") -> ToolCallResponse:
    return ToolCallResponse(content=text, tool_calls=[], raw={})


def _build_agent(llm) -> LawAgentGraph:
    """构造 LawAgentGraph（不依赖外部服务）。"""
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
    )


class TestSceneInGraph:
    """场景分类在 stream() / ask() 两条路径中都生效。"""

    def test_stream_emits_scene_event(self, monkeypatch):
        """流式路径产出场景识别事件，B 类标注「需确认」"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        agent = _build_agent(FakeToolLLM([_final_response()]))

        events = list(agent.stream("帮我起草一份房屋租赁合同"))
        scene_events = [e for e in events if e.get("type") == "thinking" and "场景识别" in (e.get("content") or "")]
        assert len(scene_events) == 1, f"场景识别事件应恰好 1 条，实际: {scene_events}"
        assert "合同起草" in scene_events[0]["content"]
        assert "B 类" in scene_events[0]["content"]
        assert "需确认" in scene_events[0]["content"]

    def test_stream_scene_event_for_a_class(self, monkeypatch):
        """A 类场景标注「全自动」"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        agent = _build_agent(FakeToolLLM([_final_response()]))

        events = list(agent.stream("劳动合同法第四十六条是什么"))
        scene_events = [e for e in events if e.get("type") == "thinking" and "场景识别" in (e.get("content") or "")]
        assert len(scene_events) == 1
        assert "法律检索" in scene_events[0]["content"]
        assert "全自动" in scene_events[0]["content"]

    def test_casual_query_skips_scene_classification(self, monkeypatch):
        """闲聊在场景分类前就 return，不应产出场景识别事件"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        agent = _build_agent(FakeToolLLM([_final_response()]))

        events = list(agent.stream("你好"))
        scene_events = [e for e in events if e.get("type") == "thinking" and "场景识别" in (e.get("content") or "")]
        assert scene_events == []

    def test_ask_writes_scene_into_state(self, monkeypatch):
        """非流式路径把场景分类结果写入 state"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        agent = _build_agent(FakeToolLLM([_final_response()]))

        result = agent.ask("帮我审查一下这份劳动合同有没有问题")
        assert result.get("scene_id") == "contract_review"
        assert result.get("scene_kind") == KIND_B
        assert result.get("scene_matched") is True

    def test_ask_fallback_scene_in_state(self, monkeypatch):
        """未命中场景时 state 中是保守回落的 A 类场景"""
        monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", True)
        agent = _build_agent(FakeToolLLM([_final_response()]))

        result = agent.ask("最近有什么新规定吗")
        # 该查询若命中法规动态追踪则为 A 类；无论命中与否都不能是 B 类
        assert result.get("scene_kind") == KIND_A
        assert result.get("scene_id") in scene_ids()
