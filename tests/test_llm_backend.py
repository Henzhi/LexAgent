"""
LLM 后端抽象层基础验证测试。

不涉及实际模型调用，仅验证:
  1. 所有模块可正确导入
  2. 各后端可实例化
  3. 工厂函数在 Ollama 和 OpenAI 模式下均正确创建
  4. 上下文窗口返回正确
"""
from __future__ import annotations

import os
from unittest.mock import patch


class TestLLMBackendImports:
    """验证模块可正确导入"""

    def test_import_base(self):
        from src.llm.base import LLMBackend
        assert LLMBackend is not None

    def test_import_ollama_backend(self):
        from src.llm.ollama_backend import OllamaBackend, OllamaLangChainWrapper
        assert OllamaBackend is not None
        assert OllamaLangChainWrapper is not None

    def test_import_openai_backend(self):
        from src.llm.openai_backend import OpenAICompatibleBackend
        assert OpenAICompatibleBackend is not None

    def test_import_factory(self):
        from src.llm.factory import create_llm_backend, LAW_SYSTEM_PROMPT
        assert create_llm_backend is not None
        assert len(LAW_SYSTEM_PROMPT) > 0


class TestOllamaBackend:
    """验证 OllamaBackend 实例化"""

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_instantiate(self, mock_client):
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(
            model="qwen2.5:7b",
            base_url="http://localhost:11434",
        )
        assert backend.model == "qwen2.5:7b"
        assert backend.base_url == "http://localhost:11434"
        assert backend.temperature == 0.1
        assert backend.max_tokens == 2048

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_context_window(self, mock_client):
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(model="qwen2.5:7b")
        assert backend.get_context_window() == 28000

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_unknown_model_default_window(self, mock_client):
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(model="unknown-model")
        assert backend.get_context_window() == 28000  # 默认值

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_qwen25_3b_window(self, mock_client):
        """qwen2.5:3b 应映射到真实上下文(32000 保守值)"""
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(model="qwen2.5:3b")
        assert backend.get_context_window() == 32000
        assert backend.num_ctx == 32000  # num_ctx 同步生效

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_num_ctx_auto_from_window(self, mock_client):
        """num_ctx 默认 0 → 自动使用模型声明窗口"""
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(model="qwen2.5:7b")
        assert backend.num_ctx == 28000  # 与 get_context_window() 一致

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_num_ctx_explicit_override(self, mock_client):
        """显式指定 num_ctx 覆盖声明窗口（如显存受限时调小）"""
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(model="qwen2.5:7b", num_ctx=4096)
        assert backend.num_ctx == 4096

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_num_ctx_passed_to_options(self, mock_client):
        """num_ctx 实际下发到 Ollama options（同步调用）"""
        from src.llm.ollama_backend import OllamaBackend
        backend = OllamaBackend(model="qwen2.5:7b", num_ctx=8192)
        backend._generate_impl([{"role": "user", "content": "测试"}])
        # 验证传给 ollama client 的 options 含 num_ctx
        _, kwargs = mock_client.return_value.chat.call_args
        assert kwargs["options"]["num_ctx"] == 8192

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_langchain_wrapper(self, mock_client):
        from src.llm.ollama_backend import OllamaBackend, OllamaLangChainWrapper
        backend = OllamaBackend(model="qwen2.5:7b")
        wrapper = OllamaLangChainWrapper(backend)
        assert wrapper.model_name == "qwen2.5:7b"
        assert wrapper._llm_type == "ollama-law-llm"


class TestOpenAIBackend:
    """验证 OpenAICompatibleBackend 实例化"""

    @patch("src.llm.openai_backend.OpenAI")
    def test_instantiate(self, mock_openai):
        from src.llm.openai_backend import OpenAICompatibleBackend
        backend = OpenAICompatibleBackend(
            model="deepseek-chat",
            api_key="sk-test123",
        )
        assert backend.model == "deepseek-chat"
        assert backend.api_key == "sk-test123"
        assert backend.temperature == 0.1

    @patch("src.llm.openai_backend.OpenAI")
    def test_context_window(self, mock_openai):
        from src.llm.openai_backend import OpenAICompatibleBackend
        backend = OpenAICompatibleBackend(model="deepseek-chat", api_key="sk-test")
        assert backend.get_context_window() == 60000

    @patch("src.llm.openai_backend.OpenAI")
    def test_default_context_window(self, mock_openai):
        from src.llm.openai_backend import OpenAICompatibleBackend
        backend = OpenAICompatibleBackend(model="unknown-model", api_key="sk-test")
        assert backend.get_context_window() == 32000  # 默认值


class TestFactory:
    """验证工厂函数"""

    @patch("src.llm.ollama_backend.ollama.Client")
    def test_create_ollama(self, mock_client):
        from src.llm.factory import create_llm_backend
        backend = create_llm_backend(backend_type="ollama", model="qwen2.5:7b")
        from src.llm.ollama_backend import OllamaBackend
        assert isinstance(backend, OllamaBackend)
        assert backend.model == "qwen2.5:7b"

    @patch("src.llm.openai_backend.OpenAI")
    def test_create_openai(self, mock_openai):
        from src.llm.factory import create_llm_backend
        backend = create_llm_backend(
            backend_type="openai",
            model="deepseek-chat",
            api_key="sk-test123",
        )
        from src.llm.openai_backend import OpenAICompatibleBackend
        assert isinstance(backend, OpenAICompatibleBackend)
        assert backend.model == "deepseek-chat"

    @patch.dict(os.environ, {"LLM_BACKEND": "ollama"})
    @patch("src.llm.ollama_backend.ollama.Client")
    def test_create_from_env_ollama(self, mock_client):
        from src.llm.factory import create_llm_backend
        from src.llm.ollama_backend import OllamaBackend
        backend = create_llm_backend()
        assert isinstance(backend, OllamaBackend)

    @patch.dict(os.environ, {"LLM_BACKEND": "ollama", "OLLAMA_NUM_CTX": "4096"})
    @patch("src.llm.ollama_backend.ollama.Client")
    def test_create_ollama_from_env_num_ctx(self, mock_client):
        """OLLAMA_NUM_CTX 环境变量 → 后端 num_ctx"""
        from src.llm.factory import create_llm_backend
        backend = create_llm_backend(model="qwen2.5:7b")
        assert backend.num_ctx == 4096

    @patch.dict(os.environ, {"LLM_BACKEND": "openai", "OPENAI_API_KEY": "sk-test"})
    @patch("src.llm.openai_backend.OpenAI")
    def test_create_from_env_openai(self, mock_openai):
        from src.llm.factory import create_llm_backend
        from src.llm.openai_backend import OpenAICompatibleBackend
        backend = create_llm_backend()
        assert isinstance(backend, OpenAICompatibleBackend)

    def test_invalid_backend(self):
        from src.llm.factory import create_llm_backend
        import pytest
        with pytest.raises(ValueError, match="不支持的 LLM 后端类型"):
            create_llm_backend(backend_type="invalid")


class TestBaseClass:
    """验证抽象基类约束"""

    def test_cannot_instantiate_abstract(self):
        import pytest
        from src.llm.base import LLMBackend
        with pytest.raises(TypeError):
            LLMBackend(model="test")

    def test_build_messages(self):
        from src.llm.base import LLMBackend

        class TestBackend(LLMBackend):
            def _generate_impl(self, messages):
                return "test"
            def _stream_impl(self, messages):
                yield "test"
            def get_context_window(self):
                return 1000

        backend = TestBackend(model="test")
        msgs = backend._build_messages(
            user_message="你好",
            history=[{"role": "assistant", "content": "你好"}],
            system_prompt="你是助手",
        )
        assert len(msgs) == 3
        assert msgs[0] == {"role": "system", "content": "你是助手"}
        assert msgs[1] == {"role": "assistant", "content": "你好"}
        assert msgs[2] == {"role": "user", "content": "你好"}

    def test_build_rag_prompt(self):
        from src.llm.base import LLMBackend
        prompt = LLMBackend._build_rag_prompt(
            query="什么是正当防卫",
            context="《刑法》第二十条...",
        )
        assert "正当防卫" in prompt
        assert "刑法" in prompt
        assert "相关法律条文" in prompt


class TestAdapterHistoryNormalization:
    """验证适配器能正确处理旧 LLMMessage 对象"""

    def test_normalize_llmmessage_list(self):
        from src.llm.adapter import _normalize_history
        from src.llm.client import Message as LLMMessage

        history = [
            LLMMessage("user", "你好"),
            LLMMessage("assistant", "你好，请问有什么法律问题？"),
        ]
        result = _normalize_history(history)
        assert result == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，请问有什么法律问题？"},
        ]

    def test_normalize_dict_list_passthrough(self):
        from src.llm.adapter import _normalize_history

        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好"},
        ]
        result = _normalize_history(history)
        assert result == history  # dict 原样通过

    def test_normalize_none(self):
        from src.llm.adapter import _normalize_history

        assert _normalize_history(None) is None

    def test_normalize_mixed_list(self):
        from src.llm.adapter import _normalize_history
        from src.llm.client import Message as LLMMessage

        history = [
            LLMMessage("user", "问题"),
            {"role": "assistant", "content": "回答"},
        ]
        result = _normalize_history(history)
        assert result == [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ]

    def test_normalize_plain_string_fallback(self):
        # 纯字符串（不应该出现但防御性处理）
        from src.llm.adapter import _normalize_history

        history = ["你好"]
        result = _normalize_history(history)
        assert result == [{"role": "user", "content": "你好"}]

    def test_adapter_passes_normalized_history(self):
        """端到端：适配器收到 LLMMessage 历史 → 后端收到 dict"""
        from src.llm.adapter import LLMAdapter
        from src.llm.client import Message as LLMMessage

        # 用一个最小可调用的假后端
        class FakeBackend:
            model = "fake"
            temperature = 0.1
            def chat(self, user_message, history=None, system_prompt=None):
                self._last_history = history
                return "ok"
            def chat_stream(self, user_message, history=None, system_prompt=None):
                self._last_history = history
                yield "ok"
            def get_context_window(self):
                return 1000
            def _build_rag_prompt(self, q, ctx):
                return f"RAG: {q}"

        backend = FakeBackend()
        adapter = LLMAdapter(backend)

        # 用旧 LLMMessage 对象
        history = [LLMMessage("user", "之前的问题")]
        adapter.chat("新问题", history=history)
        # 验证后端收到的是 dict 格式
        assert backend._last_history == [{"role": "user", "content": "之前的问题"}]
