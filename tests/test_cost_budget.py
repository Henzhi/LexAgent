"""
M3 / F14 预算熔断测试：日用量统计、超限熔断、Redis 降级、跨天重置。

不依赖真实 Redis：CostBudget 在无 redis_url 时自动走进程内计数。
"""
from __future__ import annotations

import pytest

from src.observability.cost_budget import (
    KIND_LLM,
    KIND_TAVILY,
    BudgetExceededError,
    CostBudget,
    get_budget,
    reset_budget,
)


@pytest.fixture
def budget():
    """无 Redis 的纯内存预算实例（每测试独立）。"""
    b = CostBudget(
        redis_url="",
        limits={KIND_LLM: 3, KIND_TAVILY: 2},
        enforce=True,
    )
    b.reset()
    return b


class TestCounting:
    def test_record_increments(self, budget):
        budget.record(KIND_LLM)
        assert budget.used(KIND_LLM) == 1
        budget.record(KIND_LLM, 2)
        assert budget.used(KIND_LLM) == 3

    def test_remaining_decreases(self, budget):
        assert budget.remaining(KIND_LLM) == 3
        budget.record(KIND_LLM)
        assert budget.remaining(KIND_LLM) == 2

    def test_unlimited_kind_returns_negative_one(self):
        b = CostBudget(redis_url="", limits={KIND_LLM: 0}, enforce=True)
        assert b.limit_of(KIND_LLM) == 0
        assert b.remaining(KIND_LLM) == -1
        assert b.is_exceeded(KIND_LLM) is False
        # 不限制时无论记多少都不熔断
        for _ in range(100):
            b.record(KIND_LLM)
            b.check(KIND_LLM)

    def test_kinds_are_independent(self, budget):
        """LLM 与 Tavily 分开计数、分开熔断。"""
        budget.record(KIND_LLM, 3)
        assert budget.is_exceeded(KIND_LLM) is True
        assert budget.is_exceeded(KIND_TAVILY) is False

    def test_unknown_kind_ignored(self, budget):
        budget.record("nonexistent", 5)
        assert budget.used("nonexistent") == 0


class TestCircuitBreaking:
    def test_check_passes_before_limit(self, budget):
        for _ in range(3):
            budget.check(KIND_LLM)      # 不应抛
            budget.record(KIND_LLM)

    def test_check_raises_at_limit(self, budget):
        for _ in range(3):
            budget.check(KIND_LLM)
            budget.record(KIND_LLM)
        with pytest.raises(BudgetExceededError) as exc:
            budget.check(KIND_LLM)
        assert exc.value.kind == KIND_LLM
        assert exc.value.used == 3
        assert exc.value.limit == 3

    def test_error_message_is_user_readable(self, budget):
        for _ in range(3):
            budget.check(KIND_LLM)
            budget.record(KIND_LLM)
        with pytest.raises(BudgetExceededError) as exc:
            budget.check(KIND_LLM)
        msg = str(exc.value)
        assert "3/3" in msg
        assert "次日零点" in msg

    def test_enforce_false_only_warns(self):
        """BUDGET_ENFORCE=false：超限不抛异常（观察期用）。"""
        b = CostBudget(redis_url="", limits={KIND_LLM: 1}, enforce=False)
        b.reset()
        b.record(KIND_LLM)
        b.check(KIND_LLM)                       # 不应抛
        assert b.is_exceeded(KIND_LLM) is True

    def test_tavily_breaks_independently(self, budget):
        """Tavily 超限不影响 LLM 可用（降级而非整体熔断）。"""
        for _ in range(2):
            budget.check(KIND_TAVILY)
            budget.record(KIND_TAVILY)
        with pytest.raises(BudgetExceededError):
            budget.check(KIND_TAVILY)
        budget.check(KIND_LLM)                  # LLM 仍可用


class TestResetAndStatus:
    def test_reset_clears_counts(self, budget):
        budget.record(KIND_LLM, 3)
        assert budget.is_exceeded(KIND_LLM)
        budget.reset(KIND_LLM)
        assert budget.used(KIND_LLM) == 0
        assert budget.is_exceeded(KIND_LLM) is False
        budget.check(KIND_LLM)                  # 重置后可继续

    def test_reset_all(self, budget):
        budget.record(KIND_LLM, 3)
        budget.record(KIND_TAVILY, 2)
        budget.reset()
        assert budget.used(KIND_LLM) == 0
        assert budget.used(KIND_TAVILY) == 0

    def test_status_shape(self, budget):
        budget.record(KIND_LLM)
        st = budget.status()
        assert st["storage"] == "memory"
        assert st["enforce"] is True
        assert st["exceeded"] is False
        assert st["detail"][KIND_LLM] == {
            "used": 1, "limit": 3, "remaining": 2, "exceeded": False,
        }
        assert st["detail"][KIND_TAVILY]["used"] == 0

    def test_status_reflects_exceeded(self, budget):
        budget.record(KIND_TAVILY, 2)
        st = budget.status()
        assert st["exceeded"] is True
        assert st["detail"][KIND_TAVILY]["exceeded"] is True


class TestRedisDegradation:
    def test_bad_redis_url_falls_back_to_memory(self):
        """Redis 不可用时退化为进程内计数，不抛异常、不影响主链路。"""
        b = CostBudget(
            redis_url="redis://127.0.0.1:1/0",   # 必然连不上
            limits={KIND_LLM: 5},
            enforce=True,
        )
        assert b.status()["storage"] == "memory"
        b.record(KIND_LLM)
        assert b.used(KIND_LLM) == 1

    def test_client_error_falls_back_to_memory(self, budget, monkeypatch):
        """Redis 客户端抛异常时回退内存计数，record 不向上传播异常。"""

        class _BrokenClient:
            def get(self, *a, **kw):
                raise RuntimeError("redis down")

            def pipeline(self, *a, **kw):
                raise RuntimeError("redis down")

        object.__setattr__(budget, "_client", _BrokenClient())
        budget.record(KIND_LLM)          # 不抛异常
        assert budget.used(KIND_LLM) >= 1
        assert budget.status()["storage"] == "redis"


class TestLLMBackendIntegration:
    """LLM 基类埋点：chat / chat_stream / chat_with_tools 均受预算管控。"""

    def _backend(self, limit: int):
        from src.llm.base import LLMBackend

        class _Dummy(LLMBackend):
            def _generate_impl(self, messages):
                return "ok"

            def _stream_impl(self, messages):
                yield "a"
                yield "b"

            def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
                from src.llm.base import ToolCallResponse

                return ToolCallResponse(content="ok", tool_calls=[], raw={})

            def get_context_window(self):
                return 1000

        return _Dummy(model="dummy")

    def test_chat_consumes_quota(self, monkeypatch):
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_LLM_CALLS_PER_DAY", 2)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        b = get_budget()
        b.reset(KIND_LLM)

        llm = self._backend(2)
        llm.chat("你好")
        assert b.used(KIND_LLM) == 1
        llm.chat("再问一次")
        assert b.used(KIND_LLM) == 2

        from src.observability.cost_budget import BudgetExceededError

        with pytest.raises(BudgetExceededError):
            llm.chat("第三次")
        reset_budget()

    def test_chat_stream_consumes_one_quota(self, monkeypatch):
        """流式一次调用只计一次（不是每个 token 一次）。"""
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_LLM_CALLS_PER_DAY", 10)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        b = get_budget()
        b.reset(KIND_LLM)

        llm = self._backend(10)
        tokens = list(llm.chat_stream("你好"))
        assert tokens == ["a", "b"]
        assert b.used(KIND_LLM) == 1
        reset_budget()

    def test_chat_with_tools_consumes_quota(self, monkeypatch):
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_LLM_CALLS_PER_DAY", 1)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        b = get_budget()
        b.reset(KIND_LLM)

        llm = self._backend(1)
        llm.chat_with_tools([{"role": "user", "content": "q"}], [])
        assert b.used(KIND_LLM) == 1

        from src.observability.cost_budget import BudgetExceededError

        with pytest.raises(BudgetExceededError):
            llm.chat_with_tools([{"role": "user", "content": "q"}], [])
        reset_budget()

    def test_disabled_budget_never_blocks(self, monkeypatch):
        """BUDGET_ENABLED=false 时不做任何拦截。"""
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", False)
        reset_budget()

        llm = self._backend(0)
        for _ in range(50):
            llm.chat("q")           # 不应抛
        reset_budget()


class TestTavilyIntegration:
    def test_search_raises_when_budget_exhausted(self, monkeypatch):
        from unittest.mock import MagicMock

        from src.observability.cost_budget import BudgetExceededError
        from src.search.tavily import TavilySearchClient

        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_TAVILY_CALLS_PER_DAY", 1)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        b = get_budget()
        b.reset(KIND_TAVILY)

        client = TavilySearchClient(api_key="k")
        client._client = MagicMock()
        client._client.search.return_value = {"results": []}

        client.search("q")                       # 第一次：消耗配额
        assert b.used(KIND_TAVILY) == 1
        with pytest.raises(BudgetExceededError):
            client.search("q")                   # 第二次：熔断
        reset_budget()

    def test_failed_search_does_not_consume_quota(self, monkeypatch):
        """搜索失败不计数（按成功调用计费）。"""
        from unittest.mock import MagicMock

        from src.search.tavily import TavilySearchClient

        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_TAVILY_CALLS_PER_DAY", 5)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()
        b = get_budget()
        b.reset(KIND_TAVILY)

        client = TavilySearchClient(api_key="k")
        client._client = MagicMock()
        client._client.search.side_effect = RuntimeError("api error")

        with pytest.raises(RuntimeError, match="Tavily 搜索失败"):
            client.search("q")
        assert b.used(KIND_TAVILY) == 0
        reset_budget()


class TestWebSearchToolDegradation:
    """Tavily 预算用尽 → 工具降级为 ok=False，不中断 ReAct 循环。"""

    def test_budget_exhausted_returns_ok_false(self, monkeypatch):
        from unittest.mock import MagicMock

        from src.agents.tools.web_search import build_web_search_spec
        from src.search.tavily import TavilySearchClient

        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_MAX_TAVILY_CALLS_PER_DAY", 0)  # 0 = 不限
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        reset_budget()

        client = MagicMock(spec=TavilySearchClient)
        client.is_available.return_value = True
        spec = build_web_search_spec(client)

        # 用已耗尽的预算实例直接触发
        from src.observability.cost_budget import BudgetExceededError

        client.search.side_effect = BudgetExceededError(KIND_TAVILY, 500, 500)
        result = spec.executor(query="q")

        assert result.ok is False
        assert result.summary.startswith("搜索额度已用尽")
        assert result.source == "web"
        reset_budget()
