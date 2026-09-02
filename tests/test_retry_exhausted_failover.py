"""
重试耗尽必须触发降级（2026-09-01 代码审查整改 · B1）。

问题：`OpenAICompatibleBackend` 重试耗尽后抛裸 `RuntimeError`，**不带 HTTP
状态码**（`openai_backend.py` 三处「已重试 N 次」分支）。而
`FailoverLLMBackend._is_non_retryable_api_error()` 只认 `status_code`，
拿不到就返回 False → 不降级，异常直接抛给用户。

后果：DeepSeek 持续 429/5xx 时本可切到本地 Ollama 兜底，实际却整条链路失败
——「有备胎但用不上」，正是 D5 设计里最不该出现的形态。

修复：重试耗尽统一抛 `LLMRetryExhaustedError`（继承 RuntimeError，携带最后
一次异常的状态码），failover 把该哨兵异常视为「主后端不可用」→ 降级。

本文件是**守护型**测试：把哨兵改回裸 RuntimeError、或删掉 failover 里的哨兵
判定，这里立刻转红。
"""

from __future__ import annotations

import pytest

from src.llm.base import LLMBackend, ToolCallResponse
from src.llm.failover import FailoverLLMBackend
from src.llm.retry import LLMRetryExhaustedError


class FakeAPIError(Exception):
    """带 HTTP 状态码的假 API 异常（模拟 openai SDK 异常形态）。"""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"API error {status_code}")


class FakeAlwaysFailModel:
    """每次调用都抛同一个异常的假 ChatModel（替代 backend._model）。"""

    def __init__(self, error: Exception):
        self.error = error

    def invoke(self, messages):
        raise self.error

    def stream(self, messages):
        raise self.error

    def bind_tools(self, tools, **kwargs):
        return self


class FakePrimary(LLMBackend):
    """可配置恒定失败的主后端。"""

    def __init__(self, error: Exception):
        super().__init__(model="primary-model")
        self.error = error
        self.calls = 0

    def _generate_impl(self, messages):
        self.calls += 1
        raise self.error

    def _stream_impl(self, messages):
        self.calls += 1
        raise self.error

    def get_context_window(self):
        return 1000

    def _chat_with_tools_impl(self, messages, tools, tool_choice="auto"):
        self.calls += 1
        raise self.error


class FakeOllama(LLMBackend):
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
# 哨兵异常自身
# ---------------------------------------------------------------------------


class TestRetryExhaustedError:
    def test_carries_status_code_from_last_error(self):
        err = LLMRetryExhaustedError("重试耗尽", FakeAPIError(429))
        assert err.status_code == 429
        assert err.last_error is not None

    def test_status_code_none_when_last_error_has_none(self):
        err = LLMRetryExhaustedError("重试耗尽", RuntimeError("网络炸了"))
        assert err.status_code is None

    def test_is_runtime_error_subclass_for_backward_compat(self):
        """保持 RuntimeError 兼容：既有的 except RuntimeError 分支不受影响。"""
        assert issubclass(LLMRetryExhaustedError, RuntimeError)


# ---------------------------------------------------------------------------
# 后端重试耗尽 → 抛哨兵且带状态码
# ---------------------------------------------------------------------------


class TestOpenAIBackendRaisesSentinel:
    @pytest.fixture
    def backend(self, monkeypatch):
        from src.llm.openai_backend import OpenAICompatibleBackend

        # 退避等待会真睡（base=1.0，指数退避最多 3 次），测试里直接跳过
        monkeypatch.setattr("src.llm.openai_backend.wait_and_log", lambda *a, **k: None)
        return OpenAICompatibleBackend(
            model="deepseek-v4-flash",
            api_key="sk-test-only-000000000",
            base_url="http://127.0.0.1:9/v1",  # 不可达端口：确保不会真发出请求
            max_retries=3,
        )

    def test_chat_retry_exhausted_carries_status(self, backend):
        backend._model = FakeAlwaysFailModel(FakeAPIError(429))
        with pytest.raises(LLMRetryExhaustedError) as exc_info:
            backend.chat("问题")
        assert exc_info.value.status_code == 429

    def test_chat_with_tools_retry_exhausted_carries_status(self, backend):
        backend._model = FakeAlwaysFailModel(FakeAPIError(500))
        with pytest.raises(LLMRetryExhaustedError) as exc_info:
            backend.chat_with_tools([{"role": "user", "content": "hi"}], [])
        assert exc_info.value.status_code == 500

    def test_stream_retry_exhausted_carries_status(self, backend):
        backend._model = FakeAlwaysFailModel(FakeAPIError(503))
        with pytest.raises(LLMRetryExhaustedError) as exc_info:
            list(backend.chat_stream("问题"))
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# 哨兵异常 → failover 降级
# ---------------------------------------------------------------------------


class TestFailoverSwitchesOnRetryExhausted:
    def test_chat_switches_to_fallback(self):
        """持续 429 导致重试耗尽 → 降级，而不是把异常抛给用户。"""
        primary = FakePrimary(LLMRetryExhaustedError("重试耗尽", FakeAPIError(429)))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllama())
        assert backend.chat("问题") == "fallback-answer"
        assert backend.degraded is True

    def test_chat_with_tools_switch_and_replay(self):
        primary = FakePrimary(LLMRetryExhaustedError("重试耗尽", FakeAPIError(500)))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllama())
        resp = backend.chat_with_tools([{"role": "user", "content": "hi"}], [])
        assert resp.content == "fallback-tools"
        assert backend.degraded is True
        assert primary.calls == 1

    def test_stream_switch_before_yield(self):
        primary = FakePrimary(LLMRetryExhaustedError("重试耗尽", FakeAPIError(503)))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllama())
        assert list(backend.chat_stream("问题")) == ["fallback-token"]
        assert backend.degraded is True

    def test_plain_runtime_error_still_no_switch(self):
        """哨兵之外的裸 RuntimeError（编程错误）仍不降级——别把误判放大。"""
        primary = FakePrimary(RuntimeError("内部逻辑错误"))
        backend = FailoverLLMBackend(primary=primary, fallback=FakeOllama())
        with pytest.raises(RuntimeError):
            backend.chat("问题")
        assert backend.degraded is False
