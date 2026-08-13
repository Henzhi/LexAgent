"""
Token预算 + 幻觉防御 + 可观测性 单元测试。

验证:
  1. TokenBudget 计数和截断
  2. HallucinationGuard 各层检测
  3. QueryLogger 模块可导入
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestTokenBudget:
    def test_import(self):
        from src.memory.token_budget import TokenBudget
        assert TokenBudget is not None

    def test_count(self):
        from src.memory.token_budget import TokenBudget
        # "你好" 在 cl100k_base 中约 1-2 tokens
        assert TokenBudget.count("你好") > 0
        assert TokenBudget.count("") == 0

    def test_count_batch(self):
        from src.memory.token_budget import TokenBudget
        total = TokenBudget.count_batch(["你好", "世界"])
        assert total > 2

    def test_truncate(self):
        from src.memory.token_budget import TokenBudget
        long_text = "测试 " * 500
        result = TokenBudget._truncate(long_text, 10)
        assert TokenBudget.count(result) <= 10

    def test_no_truncate_when_within_limit(self):
        from src.memory.token_budget import TokenBudget
        text = "你好世界"
        result = TokenBudget._truncate(text, 100)
        assert result == text

    def test_consume_within_limit(self):
        from src.memory.token_budget import TokenBudget
        budget = TokenBudget(28000)
        text = "简短文本"
        result = budget.consume("system_prompt", text, max_tokens=800)
        assert result == text
        assert text in budget._segments.get("system_prompt", "")

    def test_consume_over_limit_truncates(self):
        from src.memory.token_budget import TokenBudget
        budget = TokenBudget(28000)
        long_text = "长文本 " * 500
        result = budget.consume("retrieval_docs", long_text, max_tokens=50)
        assert TokenBudget.count(result) <= 50

    def test_adjust_for_simple_query(self):
        from src.memory.token_budget import TokenBudget
        budget = TokenBudget(28000)
        budget.adjust_for_complexity("工伤")
        assert budget._allocation["retrieval_docs"]["tokens"] == 3000

    def test_adjust_for_comparison_query(self):
        from src.memory.token_budget import TokenBudget
        budget = TokenBudget(28000)
        budget.adjust_for_complexity("工伤和职业病有什么区别")
        assert budget._allocation["retrieval_docs"]["tokens"] == 10000

    def test_build_assembles_parts(self):
        from src.memory.token_budget import TokenBudget
        budget = TokenBudget(28000)
        prompt = budget.build("你是助手", "检索结果", "历史", "记忆", "问题")
        assert "你是助手" in prompt
        assert "检索结果" in prompt
        assert "问题" in prompt


class TestHallucinationGuard:
    def test_import(self):
        from src.memory.hallucination_guard import HallucinationGuard
        assert HallucinationGuard is not None

    def test_empty_docs_returns_fallback(self):
        from src.memory.hallucination_guard import HallucinationGuard
        result = HallucinationGuard.check_retrieval_confidence([])
        assert result is not None
        assert "未找到" in result

    def test_high_score_docs_pass(self):
        # 阈值随当前配置动态取值（9dbdd2c 将默认从 0.7 降至 0.4，勿再硬编码分数）
        from src.memory.hallucination_guard import HallucinationGuard, MIN_SIMILARITY
        docs = [{"score": MIN_SIMILARITY + 0.2}, {"score": MIN_SIMILARITY + 0.01}]
        result = HallucinationGuard.check_retrieval_confidence(docs)
        assert result is None

    def test_low_score_docs_fail(self):
        from src.memory.hallucination_guard import HallucinationGuard, MIN_SIMILARITY
        docs = [{"score": MIN_SIMILARITY - 0.05}, {"score": MIN_SIMILARITY - 0.01}]
        result = HallucinationGuard.check_retrieval_confidence(docs)
        assert result is not None
        assert "相似度" in result

    def test_safe_content_passes(self):
        from src.memory.hallucination_guard import HallucinationGuard
        result = HallucinationGuard.check_content_safety("根据《刑法》第232条...")
        assert result is None

    def test_unsafe_content_blocked(self):
        from src.memory.hallucination_guard import HallucinationGuard
        result = HallucinationGuard.check_content_safety("教你如何黑客入侵...")
        assert result is not None
        assert "不在我的服务范围内" in result

    def test_guard_blocks_low_confidence(self):
        from src.memory.hallucination_guard import HallucinationGuard
        result = HallucinationGuard.guard(
            [{"score": 0.3}],
            "根据《刑法》...",
        )
        assert result["blocked"] is True
        assert result["reason"] == "low_retrieval_confidence"

    def test_guard_passes_safe(self):
        from src.memory.hallucination_guard import HallucinationGuard
        result = HallucinationGuard.guard(
            [{"score": 0.9}],
            "根据《刑法》第232条，故意杀人...",
        )
        assert result["blocked"] is False


class TestObservability:
    def test_import_query_logger(self):
        from src.observability.query_log import QueryLogger
        assert QueryLogger is not None

    def test_import_query_trace(self):
        from src.observability.query_log import _QueryTrace
        assert _QueryTrace is not None

    def test_trace_stage_records(self):
        from src.observability.query_log import _QueryTrace
        trace = _QueryTrace("fake_conn", "user_1", "测试查询", "req_1")
        trace.stage("intent", 200)
        assert trace._stages["intent"] == 200.0

    def test_trace_set_intent(self):
        from src.observability.query_log import _QueryTrace
        trace = _QueryTrace("fake_conn", "user_1", "测试", "req_1")
        trace.set_intent("law_lookup")
        assert trace._intent == "law_lookup"


class _FakeLLM:
    """模拟 LLMAdapter：流式返回固定 token，记录调用次数"""

    def __init__(self):
        self.stream_calls = 0

    def chat_stream(self, prompt, history=None):
        self.stream_calls += 1
        yield "你好"
        yield "，请放心。"

    def chat(self, *args, **kwargs):
        return "你好"


class TestAgentStreamRetry:
    """Agent 流式校验重试：复用首次检索结果，不重复推送 sources"""

    def _make_agent(self, docs, validate_results):
        from src.agents.graph import LawAgentGraph
        from src.rag.retriever import BaseRetriever

        llm = _FakeLLM()
        agent = LawAgentGraph(
            retriever=MagicMock(spec=BaseRetriever),
            llm=llm,
            top_k=5,
            max_retries=1,
        )

        retrieve_calls = {"n": 0}

        def fake_retrieve(state):
            retrieve_calls["n"] += 1
            return {"retrieved_docs": docs}

        validate_calls = {"n": 0}

        def fake_validate(state):
            result = validate_results[min(validate_calls["n"], len(validate_results) - 1)]
            validate_calls["n"] += 1
            return result

        agent._nodes["retrieve"] = fake_retrieve
        agent._nodes["validate"] = fake_validate
        return agent, llm, retrieve_calls

    def test_retry_reuses_retrieval_and_meta_sent_once(self):
        docs = [
            {
                "law_name": "中华人民共和国刑法", "citation": "刑法第二十条",
                "chapter": "第二编", "section": "", "article_range": "第二十条",
                "content": "为了使国家、公共利益、本人或者他人的人身、财产和其他权利免受正在进行的不法侵害...",
                "chunk_type": "article",
            },
        ]
        agent, llm, retrieve_calls = self._make_agent(
            docs,
            [
                {"validation_passed": False, "validation_feedback": "引用不准确"},
                {"validation_passed": True},
            ],
        )

        events = []
        with patch("src.agents.graph.classify_query_type", return_value="law_lookup"), \
             patch("src.memory.hallucination_guard.HallucinationGuard.guard",
                   return_value={"blocked": False, "reason": ""}):
            for ev in agent.stream("行政拘留最长多久", history=[]):
                events.append(ev)

        metas = [e for e in events if e["type"] == "meta"]
        clears = [e for e in events if e["type"] == "clear"]
        tokens = [e for e in events if e["type"] == "token"]

        assert retrieve_calls["n"] == 1, "校验重试不应重新检索"
        assert len(metas) == 1, "sources 只应推送给前端一次"
        assert len(clears) == 1, "重试应触发一次 clear"
        assert sum(1 for e in tokens if e["content"] == "你好") == 2, "两次尝试各生成一次回答"
        assert metas[0]["sources"][0]["law_name"] == "中华人民共和国刑法"
        # 重试期间（clear 之后）不应再出现 meta
        first_clear_idx = next(i for i, e in enumerate(events) if e["type"] == "clear")
        assert all(e["type"] != "meta" for e in events[first_clear_idx:]), \
            "重试阶段不应再次推送 sources"
