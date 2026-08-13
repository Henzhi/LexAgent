"""
Embedding 后端抽象层基础验证测试。

不涉及实际模型调用，仅验证:
  1. 所有模块可正确导入
  2. 各后端可实例化
  3. 工厂函数在 Ollama 和 OpenAI 模式下均正确创建
  4. 批量/单条/带进度的 embedding 调用链路正确
"""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock


class TestEmbeddingBackendImports:
    """验证模块可正确导入"""

    def test_import_base(self):
        from src.embedding.base import EmbeddingBackend
        assert EmbeddingBackend is not None

    def test_import_ollama_embedder(self):
        from src.embedding.ollama_embedder import OllamaEmbedder, OllamaLangChainEmbedder
        assert OllamaEmbedder is not None
        assert OllamaLangChainEmbedder is not None

    def test_import_openai_embedder(self):
        from src.embedding.openai_embedder import OpenAIEmbedder
        assert OpenAIEmbedder is not None

    def test_import_factory(self):
        from src.embedding.factory import create_embedding_backend
        assert create_embedding_backend is not None


class TestOllamaEmbedder:
    """验证 OllamaEmbedder 实例化和方法"""

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_instantiate(self, mock_client):
        from src.embedding.ollama_embedder import OllamaEmbedder
        embedder = OllamaEmbedder(model="bge-m3")
        assert embedder.model == "bge-m3"
        assert embedder.batch_size == 32

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_get_model_name(self, mock_client):
        from src.embedding.ollama_embedder import OllamaEmbedder
        embedder = OllamaEmbedder(model="bge-m3")
        assert embedder.get_model_name() == "ollama:bge-m3"

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_embed_empty_list(self, mock_client):
        from src.embedding.ollama_embedder import OllamaEmbedder
        embedder = OllamaEmbedder(model="bge-m3")
        assert embedder.embed([]) == []

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_embed_with_mock(self, mock_client_cls):
        from src.embedding.ollama_embedder import OllamaEmbedder
        mock_client = MagicMock()
        mock_client.embed.return_value = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
        mock_client_cls.return_value = mock_client

        embedder = OllamaEmbedder(model="bge-m3")
        result = embedder.embed(["文本1", "文本2"])
        assert len(result) == 2
        assert len(result[0]) == 2
        assert result[0] == [0.1, 0.2]

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_embed_query_with_mock(self, mock_client_cls):
        from src.embedding.ollama_embedder import OllamaEmbedder
        mock_client = MagicMock()
        mock_client.embed.return_value = {"embeddings": [[0.5, 0.6]]}
        mock_client_cls.return_value = mock_client

        embedder = OllamaEmbedder(model="bge-m3")
        result = embedder.embed_query("测试")
        assert len(result) == 2
        assert result == [0.5, 0.6]

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_langchain_wrapper(self, mock_client_cls):
        from src.embedding.ollama_embedder import OllamaEmbedder, OllamaLangChainEmbedder
        mock_client = MagicMock()
        mock_client.embed.return_value = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
        mock_client_cls.return_value = mock_client

        backend = OllamaEmbedder(model="bge-m3")
        wrapper = OllamaLangChainEmbedder(backend)
        docs = wrapper.embed_documents(["文本1", "文本2"])
        assert len(docs) == 2
        query = wrapper.embed_query("查询")
        assert len(query) == 2

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_with_progress(self, mock_client_cls):
        from src.embedding.ollama_embedder import OllamaEmbedder
        mock_client = MagicMock()
        # 每次调用 embed 返回与输入等长的向量
        mock_client.embed.side_effect = lambda model, input: {
            "embeddings": [[0.1]] * len(input)
        }
        mock_client_cls.return_value = mock_client

        embedder = OllamaEmbedder(model="bge-m3", batch_size=10)
        # 45 条文本，batch_size=10 → 5 个批次
        texts = ["text"] * 45
        result = embedder.embed_with_progress(texts, show_progress=False)
        assert len(result) == 45


class TestOpenAIEmbedder:
    """验证 OpenAIEmbedder 实例化和方法"""

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_instantiate(self, mock_openai):
        from src.embedding.openai_embedder import OpenAIEmbedder
        embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test")
        assert embedder.model == "text-embedding-3-small"
        assert embedder.batch_size == 32

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_get_model_name(self, mock_openai):
        from src.embedding.openai_embedder import OpenAIEmbedder
        embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test")
        assert embedder.get_model_name() == "openai:text-embedding-3-small"

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_get_dimension_known(self, mock_openai):
        from src.embedding.openai_embedder import OpenAIEmbedder
        embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test")
        assert embedder.get_dimension() == 1536

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_get_dimension_unknown_fallback(self, mock_openai_cls):
        from src.embedding.openai_embedder import OpenAIEmbedder
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 768)]
        mock_client.embeddings.create.return_value = mock_response
        mock_openai_cls.return_value = mock_client

        embedder = OpenAIEmbedder(model="unknown-model", api_key="sk-test")
        assert embedder.get_dimension() == 768

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_embed_empty_list(self, mock_openai):
        from src.embedding.openai_embedder import OpenAIEmbedder
        embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test")
        assert embedder.embed([]) == []

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_embed_with_mock(self, mock_openai_cls):
        from src.embedding.openai_embedder import OpenAIEmbedder
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1, 0.2]), MagicMock(embedding=[0.3, 0.4])]
        mock_client.embeddings.create.return_value = mock_response
        mock_openai_cls.return_value = mock_client

        embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="sk-test")
        result = embedder.embed(["文本1", "文本2"])
        assert len(result) == 2
        assert result[0] == [0.1, 0.2]


class TestEmbeddingFactory:
    """验证工厂函数"""

    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_create_ollama(self, mock_client):
        from src.embedding.factory import create_embedding_backend
        from src.embedding.ollama_embedder import OllamaEmbedder
        backend = create_embedding_backend(backend_type="ollama", model="bge-m3")
        assert isinstance(backend, OllamaEmbedder)
        assert backend.model == "bge-m3"

    @patch("src.embedding.openai_embedder.OpenAI")
    def test_create_openai(self, mock_openai):
        from src.embedding.factory import create_embedding_backend
        from src.embedding.openai_embedder import OpenAIEmbedder
        backend = create_embedding_backend(
            backend_type="openai",
            model="text-embedding-3-small",
            api_key="sk-test123",
        )
        assert isinstance(backend, OpenAIEmbedder)
        assert backend.model == "text-embedding-3-small"

    @patch.dict(os.environ, {"EMBED_BACKEND": "ollama"})
    @patch("src.embedding.ollama_embedder.ollama.Client")
    def test_create_from_env_ollama(self, mock_client):
        from src.embedding.factory import create_embedding_backend
        from src.embedding.ollama_embedder import OllamaEmbedder
        backend = create_embedding_backend()
        assert isinstance(backend, OllamaEmbedder)

    @patch.dict(os.environ, {"EMBED_BACKEND": "openai", "OPENAI_API_KEY": "sk-test"})
    @patch("src.embedding.openai_embedder.OpenAI")
    def test_create_from_env_openai(self, mock_openai):
        from src.embedding.factory import create_embedding_backend
        from src.embedding.openai_embedder import OpenAIEmbedder
        backend = create_embedding_backend()
        assert isinstance(backend, OpenAIEmbedder)

    def test_invalid_backend(self):
        from src.embedding.factory import create_embedding_backend
        import pytest
        with pytest.raises(ValueError, match="不支持的 Embedding 后端类型"):
            create_embedding_backend(backend_type="invalid")


class TestBaseClass:
    """验证抽象基类约束"""

    def test_cannot_instantiate_abstract(self):
        import pytest
        from src.embedding.base import EmbeddingBackend
        with pytest.raises(TypeError):
            EmbeddingBackend(model="test")

    def test_empty_embed(self):
        from src.embedding.base import EmbeddingBackend

        class TestBackend(EmbeddingBackend):
            def _embed_batch_impl(self, texts):
                return [[0.1]] * len(texts)
            def get_dimension(self):
                return 1
            def get_model_name(self):
                return "test"

        backend = TestBackend(model="test")
        assert backend.embed([]) == []
