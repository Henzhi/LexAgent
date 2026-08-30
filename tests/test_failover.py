"""
M1 后端降级测试：FailoverLLMBackend（F5 / REQ-U1 / REQ-UW2 / AC-6）。

覆盖：
- 创建期降级（主后端缺失 / 工厂主后端创建失败）
- 运行期不可重试异常（4xx/认证）→ 切换备用后端 + degraded 标记
- 可重试异常（429/5xx）→ 不触发降级
- chat / chat_stream / chat_with_tools 三条路径的降级与重放
"""

from __future__ import annotations

import pytest

from src.llm.base import LLMBackend, ToolCallResponse
from src.llm.failover import FailoverLLMBackend
from src.llm.factory import create_llm_backend


class FakeAPIError(Exception):
    """带 HTTP 状态码的假 API 异常（模拟 openai SDK 异常形态）。"""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"API error {status_code}")


class FakePrimaryBackend(LLMBackend):
    """可配置失败行为的假主后端。"""

    def __init__(self, error: Exception | None = None, stream_yield_then_fail: bool = False):
        super().__init__(model="primary-model")
        self.error = error
        self.stream_yield_then_fail = stream_yield_then_fail
        self.chat_calls = 0
        self.tools_calls = 0

    def _generate_impl(self, messages):
        self.chat_calls += 1
        if self.error:
            raise self.error
        return "primary-answer"

    def _stream_impl(self, messages):
        self.chat_calls += 1
        if self.stream_yield_then_fail:
            yield "primary-partial"
        if self.error:
            raise self.error
        yield "primary-token"

    def get_context_window(self):
        return 1000

    def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
        self.tools_calls += 1
        if self.error:
            raise self.error
        return ToolCallResponse(content="primary-tools", tool_calls=[])


class FakeOllamaBackend(LLMBackend):
    """假备用后端（类名含 Ollama，便于 active_backend 标签断言）。"""

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


# ---------------------------------------------------------------------------
# 创建期降级
# ---------------------------------------------------------------------------


class TestCreationDegradation:
    def test_primary_none_initial_degraded(self):
        backend = FailoverLLMBackend(primary=None, fallback=FakeOllamaBackend())
        assert backend.degraded is True
        assert backend.active_backend == "ollama"
        assert backend.chat("问题") == "fallback-answer"

    def test_missing_fallback_raises(self):
        with pytest.raises(ValueError, match="备用后端"):
            FailoverLLMBackend(primary=FakePrimaryBackend(), fallback=None)  # type: ignore[arg-type]

    def test_factory_creation_failure_degrades(self, monkeypatch):
        """主后端创建失败（缺 Key）→ 工厂返回已降级的 FailoverLLMBackend。"""
        monkeypatch.setattr(
            "src.llm.factory._create_openai",
            lambda **kw: (_ for _ in ()).throw(ValueError("使用 OpenAI 兼容后端必须设置 OPENAI_API_KEY")),
        )
        monkeypatch.setattr("src.llm.factory._create_ollama", lambda **kw: FakeOllamaBackend())
        backend = create_llm_backend(failover=True)
        assert isinstance(backend, FailoverLLMBackend)
        assert backend.degraded is True
        assert backend.chat("问题") == "fallback-answer"

    def test_factory_creation_success_not_degraded(self, monkeypatch):
        monkeypatch.setattr("src.llm.factory._create_openai", lambda **kw: FakePrimaryBackend())
        monkeypatch.setattr("src.llm.factory._create_ollama", lambda **kw: FakeOllamaBackend())
        backend = create_llm_backend(failover=True)
        assert isinstance(backend, FailoverLLMBackend)
        assert backend.degraded is False
        assert backend.active_backend != "ollama"


# ---------------------------------------------------------------------------
# 运行期降级
# ---------------------------------------------------------------------------


class TestRuntimeDegradation:
    def test_non_retryable_401_switches(self):
        primary = FakePrimaryBackend(error=FakeAPIError(401))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True

    def test_non_retryable_400_switches(self):
        primary = FakePrimaryBackend(error=FakeAPIError(400))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True

    def test_retryable_429_no_switch(self):
        """429 可重试 → 不降级，异常向上抛出（由 retry.py 处理）。"""
        primary = FakePrimaryBackend(error=FakeAPIError(429))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        with pytest.raises(FakeAPIError):
            backend.chat("问题")
        assert backend.degraded is False

    def test_5xx_no_switch(self):
        primary = FakePrimaryBackend(error=FakeAPIError(500))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        with pytest.raises(FakeAPIError):
            backend.chat("问题")
        assert backend.degraded is False

    def test_plain_runtime_error_no_switch(self):
        """无状态码的通用异常（如重试耗尽后的 RuntimeError）→ 不降级。"""
        primary = FakePrimaryBackend(error=RuntimeError("重试耗尽"))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        with pytest.raises(RuntimeError):
            backend.chat("问题")
        assert backend.degraded is False

    def test_chat_with_tools_switch_and_replay(self):
        primary = FakePrimaryBackend(error=FakeAPIError(401))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        resp = backend.chat_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}])
        assert resp.content == "fallback-tools"
        assert backend.degraded is True
        assert primary.tools_calls == 1

    def test_chat_with_tools_ok_primary(self):
        primary = FakePrimaryBackend()
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        resp = backend.chat_with_tools([{"role": "user", "content": "hi"}], [])
        assert resp.content == "primary-tools"
        assert backend.degraded is False

    def test_stream_switch_before_yield(self):
        primary = FakePrimaryBackend(error=FakeAPIError(401))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        tokens = list(backend.chat_stream("问题"))
        assert tokens == ["fallback-token"]
        assert backend.degraded is True

    def test_stream_midway_failure_no_replay(self):
        """已产出内容后失败 → 不降级重放（避免重复 token），异常向上抛出。"""
        primary = FakePrimaryBackend(error=FakeAPIError(401), stream_yield_then_fail=True)
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        with pytest.raises(FakeAPIError):
            list(backend.chat_stream("问题"))
        assert backend.degraded is False

    def test_switch_happens_once(self):
        """降级后后续调用直接走备用（不再尝试主后端）。"""
        primary = FakePrimaryBackend(error=FakeAPIError(401))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllamaBackend())
        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True
        assert backend.chat("问题2") == "fallback-answer"
        assert primary.chat_calls == 1  # 只尝试了一次主后端
