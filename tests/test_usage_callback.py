"""
F15 LLMUsageCallbackHandler 单元测试。

验证（对照 docs/F15-日志与Token计费面板-技术方案.md §3）：
1. on_llm_end 有 usage_metadata → 记真实 token，不标 est；
2. DeepSeek cache 拆分三级降级（usage_metadata → response_metadata.usage）；
3. 无 usage（流式/Ollama）→ tiktoken 估算标 est；
4. on_llm_error 不记（与 F14「未真正完成不占配额」一致）；
5. 采集异常不向上抛（观测故障不拖垮主链路）；
6. 两个真实后端 ChatModel 都挂了 usage callback（防漏挂）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.llm.usage_callback import LLMUsageCallbackHandler, usage_callbacks


class FakeAIMessage:
    """模拟带 usage_metadata 的 AIMessage"""

    def __init__(self, usage_metadata=None, response_metadata=None, content="答"):
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}
        self.content = content


def _handler(**kw) -> LLMUsageCallbackHandler:
    return LLMUsageCallbackHandler(backend="deepseek", model="deepseek-v4-flash", **kw)


class TestUsageMetadataPath:
    def test_records_real_tokens_no_est(self):
        cb = _handler()
        msg = FakeAIMessage(
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_tokens": 1200,
                "input_token_details": {"cache_read": 800},
            }
        )
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec:
            cb.on_llm_end(msg)
        _, kw = mock_rec.call_args
        assert kw["prompt_tokens"] == 1000
        assert kw["completion_tokens"] == 200
        assert kw["cache_hit_tokens"] == 800
        assert kw["cache_miss_tokens"] == 200
        assert kw["est"] is False
        assert kw["model"] == "deepseek-v4-flash"
        assert kw["backend"] == "deepseek"

    def test_cache_split_fallback_to_response_metadata(self):
        """usage_metadata 无 cache 详情 → 从 response_metadata.usage 读 DeepSeek 专有字段"""
        cb = _handler()
        msg = FakeAIMessage(
            usage_metadata={"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000},
            response_metadata={"usage": {"prompt_cache_hit_tokens": 500, "prompt_tokens": 900}},
        )
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec:
            cb.on_llm_end(msg)
        _, kw = mock_rec.call_args
        assert kw["cache_hit_tokens"] == 500
        assert kw["cache_miss_tokens"] == 400

    def test_no_cache_info_marks_all_miss(self):
        """拆不到 cache → 输入全按未命中计（保守），est=False（真实 total 仍有效）"""
        cb = _handler()
        msg = FakeAIMessage(usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec:
            cb.on_llm_end(msg)
        _, kw = mock_rec.call_args
        assert kw["cache_hit_tokens"] == 0
        assert kw["cache_miss_tokens"] == 100
        assert kw["est"] is False

    def test_cache_hit_clamped_to_prompt(self):
        """异常数据兜底：cache_hit 不得超过 prompt"""
        cb = _handler()
        msg = FakeAIMessage(
            usage_metadata={
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "input_token_details": {"cache_read": 9999},
            }
        )
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec:
            cb.on_llm_end(msg)
        _, kw = mock_rec.call_args
        assert kw["cache_hit_tokens"] == 50
        assert kw["cache_miss_tokens"] == 0


class TestEstimatePath:
    def test_no_usage_estimates_and_marks_est(self):
        cb = _handler()
        # on_llm_start 缓存 prompt → on_llm_end 无 usage → 估算
        cb.on_llm_start({}, ["请解释"], run_id="r1")
        msg = FakeAIMessage(usage_metadata=None, content="回答内容若干字")
        with (
            patch("src.observability.usage_store.record_llm_usage") as mock_rec,
            patch("src.memory.token_budget.TokenBudget.count", side_effect=[10, 8]) as mock_count,
        ):
            cb.on_llm_end(msg, run_id="r1")
        _, kw = mock_rec.call_args
        assert kw["est"] is True
        assert kw["prompt_tokens"] == 10
        assert kw["completion_tokens"] == 8
        assert kw["cache_miss_tokens"] == 10  # 估算全按未命中
        assert mock_count.call_count == 2

    def test_llm_result_legacy_shape(self):
        """兼容老版本 callback 传 LLMResult（generations[0][0].message）"""
        cb = _handler()
        msg = FakeAIMessage(usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        llm_result = SimpleNamespace(generations=[[SimpleNamespace(message=msg)]])
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec:
            cb.on_llm_end(llm_result)
        assert mock_rec.called


class TestErrorNoRecord:
    def test_on_llm_error_cleans_and_does_not_record(self):
        cb = _handler()
        cb.on_llm_start({}, ["请解释"], run_id="r1")
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec:
            cb.on_llm_error(RuntimeError("boom"), run_id="r1")
        mock_rec.assert_not_called()
        # 清理后同 run 的 end 不应误记
        msg = FakeAIMessage(usage_metadata={"input_tokens": 1, "output_tokens": 1})
        with patch("src.observability.usage_store.record_llm_usage") as mock_rec2:
            cb.on_llm_end(msg, run_id="r1")  # 无缓存 prompt → 估算也需 prompt；有 usage → 走 usage
        assert mock_rec2.called  # 有 usage 就记；无 run 缓存只影响估算路径


class TestFailureIsolated:
    def test_exception_in_end_swallowed(self):
        cb = _handler()
        msg = FakeAIMessage(usage_metadata={"input_tokens": 1, "output_tokens": 1})
        with patch("src.observability.usage_store.record_llm_usage", side_effect=RuntimeError("db down")):
            cb.on_llm_end(msg)  # 不应抛
        assert True

    def test_start_cache_bounded(self):
        cb = _handler()
        # 大量 start 不爆内存（超上限丢最旧）
        for i in range(300):
            cb.on_llm_start({}, [f"prompt-{i}"], run_id=f"r{i}")
        assert len(cb._start_prompts) <= 256


class TestMountedOnRealBackends:
    def test_usage_callback_mounted_on_real_backends(self):
        """两个真实后端的 ChatModel 必须挂着 usage callback（防漏挂）。

        漏挂的后果：token 永不落库，计费面板金额恒为 0——比次数熔断漏挂更隐蔽。
        """
        from src.llm.ollama_backend import OllamaBackend
        from src.llm.openai_backend import OpenAICompatibleBackend

        backends = [
            OpenAICompatibleBackend(model="deepseek-v4-flash", api_key="sk-test"),
            OllamaBackend(model="qwen2.5:7b"),
        ]
        for backend in backends:
            names = [getattr(cb, "name", type(cb).__name__) for cb in (backend.chat_model.callbacks or [])]
            assert "llm_budget_callback" in names, f"{type(backend).__name__} 未挂载预算 callback"
            assert "llm_usage_callback" in names, f"{type(backend).__name__} 未挂载 usage callback"

    def test_usage_callbacks_factory(self):
        cbs = usage_callbacks(backend="ollama", model="qwen2.5:7b")
        assert len(cbs) == 1
        assert cbs[0].name == "llm_usage_callback"
        assert cbs[0].backend == "ollama"
