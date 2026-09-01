"""
F14 预算熔断「守护型」回归测试（2026-09-01 代码审查整改）。

与 `test_cost_budget.py` 的分工：那边测的是 CostBudget 的**记账逻辑**，
这里测的是**熔断信号能否真正中断 LLM 调用链**——即审查报告发现的头号高危问题。

背景（历史 Bug，勿删注释）：
    `LLMBudgetCallbackHandler.on_llm_start` 抛出 `BudgetExceededError` 后，
    LangChain 的 `CallbackManager` 会 try/except 包住 handler 调用；基类
    `BaseCallbackHandler.raise_error` 默认 **False**，于是异常被捕获后只
    `logger.warning` 就继续放行。后果：单次请求内 18~20 次 LLM 调用**全部无法
    中断**，F14 只剩请求入口的前置检查兜底，超支上限 ≈ 单次请求调用数。

    修复只需一行类属性 `raise_error = True`，但**漏删这一行不会有任何报错**——
    所以必须有下面的端到端断言：超限后真实的 `invoke()` 必须抛异常。

跑法:
    uv run pytest tests/test_f14_budget_guard.py -v
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.language_models import BaseChatModel

from src.observability.cost_budget import (
    KIND_LLM,
    BudgetExceededError,
    CostBudget,
)


@pytest.fixture
def budget(monkeypatch):
    """把全局单例替换成限量为 2 的纯内存实例（每个用例独立）。

    直接塞 `src.observability.cost_budget._budget`，callback 里的 `get_budget()`
    就会拿到它，无需 monkeypatch 一堆 config 常量。
    """
    b = CostBudget(redis_url="", limits={KIND_LLM: 2}, enforce=True)
    b.reset()
    monkeypatch.setattr("src.observability.cost_budget._budget", b)
    return b


def _fake_model(callbacks):
    """返回一个挂载了预算 callback 的「真」LangChain ChatModel。

    必须是真的 ChatModel：用 MagicMock 或裸调 `handler.on_llm_start()` 都
    绕过了 `CallbackManager`，而 Bug 恰恰出在 CallbackManager 的异常吞没上。
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    return FakeListChatModel(responses=["ok"] * 10, callbacks=callbacks)


class _ToolCapableFakeModel(BaseChatModel):
    """支持 `bind_tools` 的最小 ChatModel。

    langchain_core 内置的 FakeListChatModel 不支持 bind_tools，而 ReAct 循环
    走的就是「bind_tools → invoke」路径——只用 FakeListChatModel 会漏测它。
    这里只实现 `_generate` + `bind_tools`，CallbackManager 那一层仍是真实的。
    """

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-tool-capable"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    def bind_tools(self, tools, **kwargs):
        """真实模型会返回 RunnableBinding；这里返回 self 即可触发调用链。"""
        return self


class TestRaiseErrorContract:
    """熔断信号不被吞没的契约。"""

    def test_handler_declares_raise_error(self):
        """`raise_error = True` 必须存在——这是熔断信号能上传的前提。

        这条断言防的是「有人重构时顺手删掉这个看起来没用的类属性」。
        删了它，下面所有端到端用例都不会红得不明显（只会 warning）。
        """
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        assert LLMBudgetCallbackHandler.raise_error is True, (
            "LLMBudgetCallbackHandler.raise_error 必须为 True，"
            "否则 LangChain CallbackManager 会吞掉 BudgetExceededError，请求内熔断失效"
        )

    def test_invoke_raises_when_exhausted(self, budget):
        """端到端：配额耗尽后，真实 invoke() 必须抛 BudgetExceededError。

        这是本次修复的核心回归点——修复前这条会失败（invoke 正常返回）。
        """
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        model = _fake_model([LLMBudgetCallbackHandler()])

        # 先耗光 2 次配额
        model.invoke("问 1")
        model.invoke("问 2")
        assert budget.used(KIND_LLM) == 2

        with pytest.raises(BudgetExceededError):
            model.invoke("问 3")

    def test_invoke_succeeds_within_quota(self, budget):
        """配额内调用正常返回并计数（防止熔断改过头，把正常请求也拦了）。"""
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        model = _fake_model([LLMBudgetCallbackHandler()])
        assert model.invoke("问 1").content == "ok"
        assert budget.used(KIND_LLM) == 1

    def test_fuse_also_blocks_bound_tools_path(self, budget):
        """工具调用路径（bind_tools → invoke）同样被熔断。

        ReAct 循环走的就是这条路径，只测普通 invoke 会漏掉它。
        """
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        model = _ToolCapableFakeModel(callbacks=[LLMBudgetCallbackHandler()])
        model.invoke("问 1")
        model.invoke("问 2")

        bound = model.bind_tools(
            [
                {
                    "name": "retrieve_knowledge",
                    "description": "检索内部知识库",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]
        )
        with pytest.raises(BudgetExceededError):
            bound.invoke("查个法条")

    def test_no_double_count_when_blocked(self, budget):
        """被拦下的调用不应计数（否则超限后每次重试都在叠加用量）。"""
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        model = _fake_model([LLMBudgetCallbackHandler()])
        model.invoke("问 1")
        model.invoke("问 2")
        for _ in range(5):
            with pytest.raises(BudgetExceededError):
                model.invoke("继续问")
        assert budget.used(KIND_LLM) == 2, "被熔断的调用不应消耗配额"

    def test_enforce_false_still_allows(self, budget):
        """BUDGET_ENFORCE=false 时只告警不拦截（运维降级开关仍可用）。"""
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        budget._enforce = False
        model = _fake_model([LLMBudgetCallbackHandler()])
        model.invoke("问 1")
        model.invoke("问 2")
        assert model.invoke("问 3").content == "ok", "enforce=false 时应放行"


class TestApiFallbackMessage:
    """API 层把熔断转成友好提示（不许把异常原文糊到用户脸上）。"""

    def test_block_message_is_user_friendly(self, monkeypatch):
        from src.observability.cost_budget import CostBudget

        from src.api.routes import _budget_block_message

        # 造一个已超限的实例（enforce=True → check 抛 BudgetExceededError）
        exhausted = CostBudget(redis_url="", limits={KIND_LLM: 1}, enforce=True)
        exhausted.reset()
        exhausted.record(KIND_LLM)
        monkeypatch.setattr("src.observability.cost_budget._budget", exhausted)

        msg = _budget_block_message()
        assert msg, "超限时必须有阻断文案"
        assert "额度" in msg or "预算" in msg
        # 内部细节（Redis key、连接串、traceback）不应出现在用户可见文案里
        for leaked in ("lexagent:budget", "redis", "Traceback", "PG_CONN", "postgresql"):
            assert leaked.lower() not in msg.lower(), f"用户可见文案泄露了内部信息: {leaked}"

    def test_block_message_empty_when_ok(self, budget):
        """配额充足时不产阻断消息（返回空串）。"""
        from src.api.routes import _budget_block_message

        assert _budget_block_message() == ""
