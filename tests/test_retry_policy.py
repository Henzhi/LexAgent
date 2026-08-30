"""
重试策略回归测试。

覆盖本次修复的核心行为：
  1. is_retryable：429/5xx/网络/超时可重试；4xx 业务错误不可重试
  2. backoff_delay：指数退避 + 全抖动，延迟在 [0, cap] 内
  3. 流式已产出内容后失败不重试（避免重复 token / 重复计费），并关闭流
  4. 首 token 前 429 可重试成功
  5. 同步生成 4xx 业务错误不重试
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessageChunk

import pytest


# ---------------------------------------------------------------------------
# 1. 重试判定
# ---------------------------------------------------------------------------


class _FakeStatusError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _FakeRetryAfterError(_FakeStatusError):
    def __init__(self, retry_after: str):
        super().__init__(429)
        self.headers = {"Retry-After": retry_after}


class TestIsRetryable:
    def test_429_retryable(self):
        from src.llm.retry import is_retryable

        assert is_retryable(_FakeStatusError(429)) is True

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_retryable(self, status):
        from src.llm.retry import is_retryable

        assert is_retryable(_FakeStatusError(status)) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_4xx_not_retryable(self, status):
        from src.llm.retry import is_retryable

        assert is_retryable(_FakeStatusError(status)) is False

    def test_timeout_retryable(self):
        from src.llm.retry import is_retryable

        assert is_retryable(TimeoutError("timeout")) is True

    def test_connection_error_retryable(self):
        from src.llm.retry import is_retryable

        assert is_retryable(ConnectionError("conn refused")) is True

    def test_business_error_not_retryable(self):
        from src.llm.retry import is_retryable

        assert is_retryable(ValueError("bad arg")) is False


class TestBackoff:
    def test_delay_within_range(self):
        from src.llm.retry import backoff_delay

        for attempt in range(1, 5):
            cap = min(1.0 * (2 ** (attempt - 1)), 30.0)
            for _ in range(50):
                d = backoff_delay(attempt, base=1.0, cap=30.0)
                assert 0 <= d <= cap + 1e-6

    def test_retry_after_parsed(self):
        from src.llm.retry import get_retry_after_seconds

        assert get_retry_after_seconds(_FakeRetryAfterError("5")) == 5.0
        assert get_retry_after_seconds(_FakeStatusError(429)) is None


# ---------------------------------------------------------------------------
# 2. 流式重试行为（不 mock 重试等待，直接断言调用次数）
# ---------------------------------------------------------------------------


class TestStreamRetry:
    """流式/同步重试行为。

    D-M3-13 迁移后：实现改走 LangChain ChatModel（`backend._model`），
    因此 mock 点从 openai SDK 的 `_client.chat.completions.create`
    改为 `_model.stream` / `_model.invoke`。**重试策略本身不变**
    （仍是 is_retryable + wait_and_log，D-M1-3）。
    """

    def _backend(self):
        from src.llm.openai_backend import OpenAICompatibleBackend

        backend = OpenAICompatibleBackend(model="m", api_key="k")
        backend.max_retries = 3
        backend._model = MagicMock()
        return backend

    def test_no_retry_after_yield(self):
        """已产出内容后失败：不重试整个流（避免重复 token / 重复计费）"""
        backend = self._backend()
        calls = {"n": 0}

        def fake_stream(*args, **kwargs):
            calls["n"] += 1
            yield AIMessageChunk(content="a")
            yield AIMessageChunk(content="b")
            raise ConnectionError("mid-stream broken")

        backend._model.stream.side_effect = fake_stream

        tokens = []
        with pytest.raises(ConnectionError):
            for t in backend._stream_impl([{"role": "user", "content": "x"}]):
                tokens.append(t)
        assert tokens == ["a", "b"]
        assert calls["n"] == 1, "已产出内容后不应重试"

    def test_retry_before_first_yield_on_429(self):
        """首 token 前 429：退避后重试成功"""
        backend = self._backend()
        calls = {"n": 0}

        def fake_stream(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeStatusError(429)
            yield AIMessageChunk(content="ok")

        backend._model.stream.side_effect = fake_stream

        with patch("src.llm.retry.time.sleep") as mock_sleep:
            tokens = list(backend._stream_impl([{"role": "user", "content": "x"}]))
        assert tokens == ["ok"]
        assert calls["n"] == 2
        assert mock_sleep.call_count == 1

    def test_4xx_generate_no_retry(self):
        """同步生成：4xx 业务错误直接抛出，不重试"""
        backend = self._backend()
        calls = {"n": 0}

        def fake_invoke(*args, **kwargs):
            calls["n"] += 1
            raise _FakeStatusError(400)

        backend._model.invoke.side_effect = fake_invoke

        with pytest.raises(_FakeStatusError):
            backend._generate_impl([{"role": "user", "content": "x"}])
        assert calls["n"] == 1, "4xx 不应重试"
