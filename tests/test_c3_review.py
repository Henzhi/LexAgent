"""C3 / M4 阶段 2（D-M4-3）：审核子图——三层审核（规则守卫+LLM 审核+定向回源核验）。

退出判据关联（docs/M4-多Agent路线图.md §4 阶段 2）：
- 审核子图 = 第一个真子 Agent（独立判定契约 state.review）；
- 旧重试语义（validation_passed/validation_feedback/retry_count）完全兼容，图结构零改动；
- AGENT_REVIEW_ENABLED=false 与升级前逐字一致（逃生通道，动态求值）；
- pkulaw 定向核验只对未回源引用触发（预算护栏），法宝故障 fail-open 不阻断。
"""

from __future__ import annotations


from src.agents import graph as graph_mod
from src.agents.review import (
    article_key,
    citation_backed,
    cn_to_int,
    extract_citations,
    find_unbacked_citations,
    law_key,
    make_review_subgraph,
    parse_review_verdict,
)
from src.agents.tools import build_default_tools
from src.agents.tools.pkulaw_search import build_pkulaw_search_spec, build_pkulaw_verify_spec
from src.llm.base import ToolCallResponse
from tests.fakes import FakePkulawClient, FakeRetriever, FakeToolLLM

from tests.test_react_agent import _build_agent, _final_response

_DOC = {
    "law_name": "中华人民共和国治安管理处罚法",
    "article_range": "第十六条",
    "content": "第十六条 有两种以上违反治安管理行为的，分别决定，合并执行。行政拘留处罚合并执行的，最长不超过二十日。",
    "citation": "《中华人民共和国治安管理处罚法》第十六条",
    "score": 0.85,  # 高于置信度阈值，避免 L1 规则守卫拦截正常用例
}


def _verdict_json(verdict: str, feedback: str = "评语") -> ToolCallResponse:
    import json

    return ToolCallResponse(
        content=json.dumps({"verdict": verdict, "issues": [], "feedback": feedback}, ensure_ascii=False),
        tool_calls=[],
        raw={},
    )


# ---------------------------------------------------------------------------
# 纯函数：引用抽取 / 回源判定 / verdict 解析
# ---------------------------------------------------------------------------


class TestCitationExtraction:
    def test_cn_to_int(self):
        assert cn_to_int("二百五十一") == 251
        assert cn_to_int("16") == 16
        assert cn_to_int("十六") == 16
        assert cn_to_int("一千二百三十八") == 1238
        assert cn_to_int("xyz") is None

    def test_article_key_unifies(self):
        assert article_key("第251条") == article_key("第二百五十一条") == "251"

    def test_law_key_normalizes(self):
        assert law_key("《中华人民共和国监察法(2024修正)》") == "监察法"
        assert law_key("监察法") == "监察法"

    def test_extract_citations_pairs(self):
        text = "依据《治安管理处罚法》第十六条，合并执行最长不超过二十日；另见《监察法》第二十二条。"
        cits = extract_citations(text)
        assert [(c["law_k"], c["article_k"]) for c in cits] == [("治安管理处罚法", "16"), ("监察法", "22")]

    def test_bare_article_not_collected(self):
        assert extract_citations("第十五条规定了相关内容") == []

    def test_citation_backed_by_docs(self):
        cit = extract_citations("依据《治安管理处罚法》第十六条处理。")[0]
        assert citation_backed(cit, [_DOC]) is True
        assert citation_backed(cit, []) is False

    def test_unbacked_detection(self):
        answer = "依据《治安管理处罚法》第十六条，合并执行最长不超过二十日。另《公证法》第九十九条也相关。"
        unbacked = find_unbacked_citations(answer, [_DOC])
        assert [c["law_k"] for c in unbacked] == ["公证法"]


class TestParseReviewVerdict:
    def test_json_pass(self):
        out = parse_review_verdict('{"verdict": "pass", "issues": [], "feedback": "ok"}')
        assert out["verdict"] == "pass"

    def test_json_reject(self):
        out = parse_review_verdict('前缀{"verdict": "reject", "issues": ["编造条号"], "feedback": "引用有误"}后缀')
        assert out["verdict"] == "reject" and out["issues"] == ["编造条号"]

    def test_legacy_pass_format(self):
        """旧 validate 的 "PASS/理由" 格式兼容（FakeToolLLM 桩依赖此路径）。"""
        assert parse_review_verdict("PASS\n理由：未发现幻觉")["verdict"] == "pass"

    def test_legacy_reject_format(self):
        out = parse_review_verdict("FAIL\n理由：未引用条文")
        assert out["verdict"] == "reject" and out["feedback"] == "未引用条文"

    def test_garbage_fail_open(self):
        assert parse_review_verdict("嗯，我觉得还行")["verdict"] == "pass"


# ---------------------------------------------------------------------------
# 审核子图（编译子图直测，不触网络）
# ---------------------------------------------------------------------------


class _ChatStub:
    """审核子图专用 LLM 桩：只实现 chat()（子图唯一消费的入口），按脚本返回。

    FakeToolLLM.chat 硬编码返回 "PASS"（供旧 validate 测试用），审不到自定义
    verdict 脚本，故单测用本桩精确控制审核输出。
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.chats: list[str] = []

    def chat(self, prompt, history=None, system_prompt=None):
        self.chats.append(prompt)
        return self.replies.pop(0) if self.replies else "PASS"


def _registry_with_pkulaw(client=FakePkulawClient()):
    # conftest 已 patch 掉 PKULAW_ENABLED（防真网络），显式注册 Fake 工具供审核子图消费
    registry = build_default_tools(FakeRetriever(), pkulaw_client=client)
    if not registry.has("pkulaw_verify"):
        registry.register(build_pkulaw_search_spec(client))
        registry.register(build_pkulaw_verify_spec(client))
    return registry


def _registry_without_pkulaw():
    # conftest 隔离下 build_default_tools 不注册 pkulaw（绝不构造真客户端/真网络）
    return build_default_tools(FakeRetriever())


def _run_review(llm, registry, answer: str, docs: list[dict] | None = None, retry: int = 0) -> dict:
    graph = make_review_subgraph(llm, registry, max_retries=1)
    state = {
        "query": "行政拘留合并执行最长多久",
        "answer": answer,
        "retrieved_docs": docs if docs is not None else [_DOC],
        "retry_count": retry,
    }
    merged = graph.invoke(state)
    return {
        "review": merged.get("review") or {},
        **{k: merged.get(k) for k in ("validation_passed", "retry_count", "validation_feedback")},
    }


class TestReviewSubgraph:
    def test_llm_pass_no_unbacked(self):
        llm = _ChatStub([_verdict_json("pass").content])
        answer = "依据《治安管理处罚法》第十六条，合并执行最长不超过二十日。"
        out = _run_review(llm, _registry_without_pkulaw(), answer)
        assert out["validation_passed"] is True
        assert out["review"]["verdict"] == "pass"
        assert out["review"]["unbacked_citations"] == []

    def test_llm_reject_triggers_retry_semantics(self):
        llm = _ChatStub([_verdict_json("reject", "遗漏关键罚则").content])
        out = _run_review(llm, _registry_without_pkulaw(), "随便答的")
        assert out["validation_passed"] is False
        assert out["retry_count"] == 1
        assert out["validation_feedback"] == "遗漏关键罚则"
        assert out["review"]["verdict"] == "reject"

    def test_llm_reject_budget_exhausted_forced_pass(self):
        llm = _ChatStub([_verdict_json("reject", "仍不行").content])
        out = _run_review(llm, _registry_without_pkulaw(), "随便答的", retry=1)
        assert out["validation_passed"] is True  # 预算耗尽强制放行（与旧 validate 一致）
        assert out["review"]["verdict"] == "reject" and out["review"]["forced"] is True

    def test_llm_failure_fails_open(self):
        class BoomLLM(FakeToolLLM):
            def chat(self, *a, **k):
                raise RuntimeError("LLM 挂了")

        out = _run_review(BoomLLM([]), _registry_without_pkulaw(), "答案")
        assert out["validation_passed"] is True
        assert out["review"]["verdict"] == "pass"

    def test_rule_guard_blocks_without_llm_call(self):
        llm = FakeToolLLM([])  # 无脚本：若被调用会返回"已耗尽"答案，不该发生
        bad_docs = [{"law_name": "刑法", "article_range": "第一条", "content": "x" * 10, "citation": "《刑法》第一条"}]
        out = _run_review(llm, _registry_without_pkulaw(), "很短", docs=bad_docs)
        assert out["review"]["verdict"] == "blocked"
        assert out["review"]["layer"] == "rule"

    def test_unbacked_citation_verify_passes_with_fake_pkulaw(self):
        """未回源引用 → 走 pkulaw provision 核验；Fake 返回 match=True → 放行。"""
        llm = _ChatStub([_verdict_json("pass").content])
        registry = _registry_with_pkulaw()
        answer = "依据《民法典》第一百七十六条处理民事责任问题。"
        out = _run_review(llm, registry, answer)
        assert out["validation_passed"] is True
        assert out["review"]["unbacked_citations"] == [{"law": "民法典", "article": "第一百七十六条"}]
        assert out["review"].get("verify_note") == "已回源核验通过"

    def test_unbacked_citation_verify_mismatch_rejects(self):
        class MismatchPkulaw(FakePkulawClient):
            def verify_provision(self, userlaw, answerlaw, prompt=""):
                return {"compared": True, "match": False}

        llm = _ChatStub([_verdict_json("pass").content])
        registry = _registry_without_pkulaw()
        registry.register(build_pkulaw_verify_spec(MismatchPkulaw()))
        out = _run_review(llm, registry, "依据《不存在的法》第一条胡说。")
        assert out["validation_passed"] is False
        assert out["review"]["verdict"] == "reject"
        assert out["review"]["layer"] == "verify"

    def test_pkulaw_unavailable_fails_open(self):
        """未注册 pkulaw_verify（未配置/预算熔断同理）→ 跳过核验放行留痕。"""
        llm = _ChatStub([_verdict_json("pass").content])
        out = _run_review(llm, _registry_without_pkulaw(), "依据《某法》第一条作答。")
        assert out["validation_passed"] is True
        assert "未注册" in out["review"].get("verify_note", "")


# ---------------------------------------------------------------------------
# graph 接线：开关动态求值 + SSE review 事件
# ---------------------------------------------------------------------------


class TestGraphIntegration:
    def test_review_disabled_uses_legacy_validate(self, monkeypatch):
        """开关关闭 → 旧 validate 路径（FakeToolLLM.chat 的 PASS 文本也判 pass）。"""
        monkeypatch.setattr(graph_mod, "AGENT_REVIEW_ENABLED", False)
        monkeypatch.setattr(graph_mod, "AGENT_REACT_ENABLED", True)
        llm = FakeToolLLM([_final_response("最终答案")])
        agent = _build_agent(llm, monkeypatch)
        result = agent.ask("借款利息有什么规定", user_id="u1", session_id="s1")
        assert result["answer"] == "最终答案"
        assert not result.get("review")  # 旧路径不产 review 字段内容

    def test_review_enabled_produces_review_contract(self, monkeypatch):
        monkeypatch.setattr(graph_mod, "AGENT_REVIEW_ENABLED", True)
        monkeypatch.setattr(graph_mod, "AGENT_REACT_ENABLED", True)
        # 脚本：①最终回答（agent_node）②审核 JSON verdict
        llm = FakeToolLLM(
            [_final_response("依据《治安管理处罚法》第十六条，合并执行最长不超过二十日。"), _verdict_json("pass")]
        )
        agent = _build_agent(llm, monkeypatch)
        result = agent.ask("行政拘留合并执行最长多久", user_id="u1", session_id="s1")
        assert result["answer"]
        assert result["review"]["verdict"] == "pass"
        assert result["validation_passed"] is True

    def test_stream_review_event_carries_agent_dim(self, monkeypatch):
        monkeypatch.setattr(graph_mod, "AGENT_REVIEW_ENABLED", True)
        monkeypatch.setattr(graph_mod, "AGENT_REACT_ENABLED", True)
        llm = FakeToolLLM([_final_response("答案正文"), _verdict_json("pass")])
        agent = _build_agent(llm, monkeypatch)
        events = list(agent.stream("行政拘留合并执行最长多久", history=[], user_id="u1", session_id="s1"))
        reviews = [e for e in events if e.get("type") == "review"]
        assert reviews, "审核启用时应产出 review 事件"
        assert all(e["agent"] == "review" for e in reviews)
        assert reviews[0]["verdict"] == "pass"

    def test_stream_no_review_event_when_disabled(self, monkeypatch):
        monkeypatch.setattr(graph_mod, "AGENT_REVIEW_ENABLED", False)
        monkeypatch.setattr(graph_mod, "AGENT_REACT_ENABLED", True)
        llm = FakeToolLLM([_final_response("答案正文")])
        agent = _build_agent(llm, monkeypatch)
        events = list(agent.stream("行政拘留合并执行最长多久", history=[], user_id="u1", session_id="s1"))
        assert not [e for e in events if e.get("type") == "review"]
