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


class TestLLMBudgetCallback:
    """F14 预算埋点（D-M3-13 后改为 LangChain callback）。

    迁移前埋点写在 `LLMBackend` 的三个公开入口；迁移后统一由
    `LLMBudgetCallbackHandler` 在 LangChain 调用链路上埋点——这样即使上层
    绕过公开入口直接 `chat_model.invoke()` 也会被计数。
    """

    def _handler(self):
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        return LLMBudgetCallbackHandler()

    def _setup(self, monkeypatch, limit: int):
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        monkeypatch.setattr("src.config.BUDGET_MAX_LLM_CALLS_PER_DAY", limit)
        reset_budget()
        b = get_budget()
        b.reset(KIND_LLM)
        return b

    def test_one_call_consumes_one_quota(self, monkeypatch):
        """一次调用完成 → 计数 1"""
        b = self._setup(monkeypatch, 10)
        h = self._handler()
        h.on_llm_start({}, ["hi"])
        h.on_llm_end(None)
        assert b.used(KIND_LLM) == 1
        reset_budget()

    def test_stream_call_counts_once_not_per_token(self, monkeypatch):
        """流式一次调用只计一次——不是每个 token 一次。

        这是把埋点放 callback 上时最容易踩的坑：LangChain 每生成一个 token
        都会回调 on_llm_new_token，若在那里计数会把配额瞬间打满。
        """
        b = self._setup(monkeypatch, 10)
        h = self._handler()
        h.on_llm_start({}, ["hi"])
        for _ in range(500):  # 模拟 500 个流式 token
            h.on_llm_new_token("x", run_id="r1")
        h.on_llm_end(None)
        assert b.used(KIND_LLM) == 1, "500 个 token 也应只计一次调用"
        reset_budget()

    def test_error_does_not_consume_quota(self, monkeypatch):
        """调用失败不计数（请求未真正完成，与 D-M3-6 一致）"""
        b = self._setup(monkeypatch, 10)
        h = self._handler()
        h.on_llm_start({}, ["hi"])
        h.on_llm_error(RuntimeError("boom"))
        assert b.used(KIND_LLM) == 0
        reset_budget()

    def test_exceeded_raises_on_start(self, monkeypatch):
        """超限 → on_llm_start 抛 BudgetExceededError（熔断，中断调用链）"""
        from src.observability.cost_budget import BudgetExceededError

        b = self._setup(monkeypatch, 1)
        h = self._handler()
        h.on_llm_start({}, ["hi"])
        h.on_llm_end(None)
        assert b.used(KIND_LLM) == 1

        with pytest.raises(BudgetExceededError):
            h.on_llm_start({}, ["再问一次"])
        reset_budget()

    def test_stats_failure_does_not_block(self, monkeypatch):
        """统计组件故障 → 告警放行，不拖垮主链路（D-M3-8）"""
        b = self._setup(monkeypatch, 10)

        def _boom(*args, **kwargs):
            raise RuntimeError("redis down")

        monkeypatch.setattr(type(b), "check", _boom)
        monkeypatch.setattr(type(b), "record", _boom)

        h = self._handler()
        h.on_llm_start({}, ["hi"])  # 不应抛出
        h.on_llm_end(None)  # 不应抛出
        reset_budget()

    def test_callback_mounted_on_real_backends(self):
        """两个真实后端的 ChatModel 必须挂着预算 callback（防漏挂）。

        漏挂的后果很隐蔽：调用照常进行，只是不计数，预算熔断形同虚设。
        """
        from src.llm.ollama_backend import OllamaBackend
        from src.llm.openai_backend import OpenAICompatibleBackend

        backends = [
            OpenAICompatibleBackend(model="deepseek-v4-flash", api_key="sk-test"),
            OllamaBackend(model="qwen2.5:7b"),
        ]
        for backend in backends:
            names = [
                getattr(cb, "name", type(cb).__name__)
                for cb in (backend.chat_model.callbacks or [])
            ]
            assert "llm_budget_callback" in names, (
                f"{type(backend).__name__} 未挂载预算 callback"
            )


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
