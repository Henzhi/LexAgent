"""
降级自动回切（2026-09-01 代码审查整改 · B2）。

问题：failover 是**单向**的——一次瞬时 401/403（Key 限流、网关拦截）就让整个
进程永久切到 Ollama，没有健康探测、没有回切，只能重启服务恢复。更糟的是
`graph.py` 的 `_react_enabled` 在**构造期固化**：哪怕主后端后来恢复，ReAct
工具调用能力也回不来，用户一直走固定管线。

修复：
1. `FailoverLLMBackend` 降级后进入冷却窗口（默认 300s）。冷却结束后，下一次
   **真实请求**即作为健康探测走主后端：成功 → 回切；失败 → 继续降级并刷新
   冷却窗口（避免每个请求都去探测一个已知故障的后端）。
   - 不额外发 ping 探测请求：那要为"确认后端活着"付出真实 Token 与一次 RTT，
     而复用下一次真实请求零成本 —— 探测失败时请求仍由备用后端正常应答。
2. `LawAgentGraph._react_enabled` 改为动态属性：**能力**（开关 + 是否支持工具）
   构造期决定，「当前是否降级」每次访问时求值；两张图（ReAct / 固定管线）在
   具备能力时都预先构建，运行时按状态切换。

守护型测试：回退成单向降级、或把 `_react_enabled` 改回构造期固化，这里立刻转红。
"""

from __future__ import annotations

import time

import pytest

from src.llm.base import LLMBackend, ToolCallResponse
from src.llm.failover import FailoverLLMBackend


class FakeAPIError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"API error {status_code}")


class FakeOpenAIPrimary(LLMBackend):
    """健康状态可随时切换的主后端（类名含 openai，便于 active_backend 断言）。"""

    def __init__(self, error: Exception | None = None):
        super().__init__(model="primary-model")
        self.error = error
        self.calls = 0

    def heal(self) -> None:
        self.error = None

    def _generate_impl(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return "primary-answer"

    def _stream_impl(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        yield "primary-token"

    def get_context_window(self):
        return 1000

    def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
        self.calls += 1
        if self.error:
            raise self.error
        return ToolCallResponse(content="primary-tools", tool_calls=[])


class FakeOllamaBackend(LLMBackend):
    def __init__(self):
        super().__init__(model="qwen2.5:3b")

    def _generate_impl(self, messages):
        return "fallback-answer"

    def _stream_impl(self, messages):
        yield "fallback-token"

    def get_context_window(self):
        return 32000

    def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
        return ToolCallResponse(content="fallback-tools", tool_calls=[])


def _degrade(backend: FailoverLLMBackend, status: int = 401) -> FakeOpenAIPrimary:
    """触发一次真实降级（主后端 4xx），返回 primary 便于后续断言/改状态。"""
    primary: FakeOpenAIPrimary = backend.primary  # type: ignore[assignment]
    primary.error = FakeAPIError(status)
    assert backend.chat("问题") == "fallback-answer"
    assert backend.degraded is True
    return primary


# ---------------------------------------------------------------------------
# 冷却窗口内的探测节流
# ---------------------------------------------------------------------------


class TestProbeThrottling:
    def test_no_probe_before_cooldown(self):
        """冷却未过 → 一次都不探测（primary.calls 不再增长）。"""
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=3600
        )
        primary = _degrade(backend)
        assert backend.chat("第二个问题") == "fallback-answer"
        assert backend.chat("第三个问题") == "fallback-answer"
        assert primary.calls == 1, "冷却期内不应反复试探已知故障的主后端"

    def test_cooldown_zero_disables_recovery(self):
        """cooldown=0 → 显式禁用自动回切，保持"降级后不再回头"的旧语义。"""
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0
        )
        primary = _degrade(backend)
        primary.heal()
        time.sleep(0.05)
        assert backend.chat("问题") == "fallback-answer"
        assert primary.calls == 1
        assert backend.degraded is True

    def test_no_probe_when_primary_missing(self):
        """创建期即降级（primary=None）→ 没有可探测对象，不报错。"""
        backend = FailoverLLMBackend(primary=None, fallback=FakeOllamaBackend())
        assert backend.chat("问题") == "fallback-answer"
        assert backend.chat("问题2") == "fallback-answer"
        assert backend.degraded is True


# ---------------------------------------------------------------------------
# 冷却结束后的健康探测
# ---------------------------------------------------------------------------


class TestProbeRecovery:
    def test_probe_success_recovers_chat(self):
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0.05
        )
        primary = _degrade(backend)
        primary.heal()
        time.sleep(0.08)

        assert backend.chat("恢复后的第一个问题") == "primary-answer"
        assert backend.degraded is False
        assert backend.active_backend == "openai"
        # 回切后持续走主后端，不再回到备用
        assert backend.chat("恢复后的第二个问题") == "primary-answer"

    def test_probe_success_recovers_chat_with_tools(self):
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0.05
        )
        primary = _degrade(backend)
        primary.heal()
        time.sleep(0.08)

        resp = backend.chat_with_tools([{"role": "user", "content": "hi"}], [])
        assert resp.content == "primary-tools"
        assert backend.degraded is False

    def test_probe_success_recovers_stream(self):
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0.05
        )
        primary = _degrade(backend)
        primary.heal()
        time.sleep(0.08)

        assert list(backend.chat_stream("问题")) == ["primary-token"]
        assert backend.degraded is False


class TestProbeFailure:
    def test_still_broken_stays_degraded_and_serves_fallback(self):
        """探测失败：保持降级，但本次请求仍由备用后端正常应答（不抛给用户）。"""
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0.05
        )
        _degrade(backend)  # primary 仍是 401
        time.sleep(0.08)

        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True
        assert backend.primary.calls == 2  # type: ignore[union-attr]

    def test_probe_failure_refreshes_cooldown(self):
        """探测失败要刷新冷却窗口，否则每个请求都会去试探故障后端。"""
        backend = FailoverLLMBackend(
            primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0.3
        )
        primary = _degrade(backend)
        time.sleep(0.35)  # 冷却已过 → 探测
        backend.chat("问题")
        assert primary.calls == 2
        backend.chat("问题2")  # 探测刚失败 → 冷却已刷新，不再探测
        assert primary.calls == 2

    def test_probe_failure_with_non_4xx_also_serves_fallback(self):
        """探测期间主后端抛非 4xx（如重试耗尽）→ 同样由备用端应答，不中断用户请求。"""
        primary = FakeOpenAIPrimary()
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend(), recovery_cooldown_seconds=0.05)
        _degrade(backend)
        primary.error = RuntimeError("重试耗尽")
        time.sleep(0.08)

        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True


# ---------------------------------------------------------------------------
# graph：_react_enabled 动态求值（降级后回切要能拿回 ReAct 能力）
# ---------------------------------------------------------------------------


def _build_agent(llm, monkeypatch, react=True):
    from src.agents.graph import LawAgentGraph
    from src.agents.tools import build_default_tools
    from tests.fakes import FakeRetriever

    monkeypatch.setattr("src.agents.graph.AGENT_REACT_ENABLED", react)
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


class TestReactEnabledDynamic:
    def test_property_follows_runtime_degradation(self, monkeypatch):
        from tests.fakes import FakeToolLLM

        llm = FakeToolLLM([])
        llm.degraded = False
        agent = _build_agent(llm, monkeypatch)
        assert agent._react_enabled is True

        llm.degraded = True  # 运行期主后端故障 → 降级
        assert agent._react_enabled is False

        llm.degraded = False  # 探测成功 → 回切
        assert agent._react_enabled is True

    def test_graph_switches_back_after_recovery(self, monkeypatch):
        """构造时即降级 → 固定管线；回切后同一实例能拿回 ReAct 图。"""
        from tests.fakes import FakeToolLLM

        llm = FakeToolLLM([])
        llm.degraded = True
        agent = _build_agent(llm, monkeypatch)

        assert agent._react_enabled is False
        fixed_nodes = set(agent._graph.get_graph().nodes)
        assert "retrieve" in fixed_nodes
        assert "agent" not in fixed_nodes

        llm.degraded = False  # 主后端恢复
        assert agent._react_enabled is True
        react_nodes = set(agent._graph.get_graph().nodes)
        assert {"agent", "tools"} <= react_nodes

    def test_react_disabled_stays_false_even_when_healthy(self, monkeypatch):
        """AGENT_REACT_ENABLED=false 时，无论降级状态如何都不启用 ReAct（AC-7 回归）。"""
        from tests.fakes import FakeToolLLM

        llm = FakeToolLLM([])
        llm.degraded = False
        agent = _build_agent(llm, monkeypatch, react=False)
        assert agent._react_enabled is False
        llm.degraded = True
        assert agent._react_enabled is False


@pytest.mark.parametrize("cooldown", [0.05, 1.0])
def test_recovery_cooldown_exposed(cooldown):
    """冷却窗口可按部署形态配置（构造参数透出，便于运维调参）。"""
    backend = FailoverLLMBackend(
        primary=FakeOpenAIPrimary(), fallback=FakeOllamaBackend(), recovery_cooldown_seconds=cooldown
    )
    assert backend.recovery_cooldown_seconds == cooldown
