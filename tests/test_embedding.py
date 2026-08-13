"""Embedding 模块单元测试 — LawEmbedder 批处理与重试逻辑（mock ollama）"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_ollama_embed():
    """mock ollama.embed 返回固定 1024 维向量"""
    with patch("ollama.Client", autospec=True) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # mock embed 方法：返回 [0.1] * 1024
        def fake_embed(model, input, **kwargs):
            # input 可以是 str 或 list[str]
            if isinstance(input, list):
                return {"embeddings": [[0.1] * 1024 for _ in input]}
            return {"embedding": [0.1] * 1024}

        mock_client.embed.side_effect = fake_embed

        from src.embedding.embedder import LawEmbedder
        embedder = LawEmbedder(model="bge-m3", base_url="http://localhost:11434", batch_size=8)
        yield embedder


class TestLawEmbedder:
    """LawEmbedder 单元测试"""

    def test_embed_documents_single_batch(self, mock_ollama_embed):
        texts = ["测试文本1", "测试文本2", "测试文本3"]
        embeddings = mock_ollama_embed.embed_documents(texts)
        assert len(embeddings) == 3
        for emb in embeddings:
            assert len(emb) == 1024
            assert abs(emb[0] - 0.1) < 0.01

    def test_embed_documents_empty(self, mock_ollama_embed):
        embeddings = mock_ollama_embed.embed_documents([])
        assert embeddings == []

    def test_embed_query(self, mock_ollama_embed):
        query = "行政处罚的种类"
        embedding = mock_ollama_embed.embed_query(query)
        assert len(embedding) == 1024
        assert abs(embedding[0] - 0.1) < 0.01

    def test_batch_splitting(self, mock_ollama_embed):
        """验证超过 batch_size 时正确分批"""
        # batch_size=8, 提交 20 条
        texts = [f"文本{i}" for i in range(20)]
        embeddings = mock_ollama_embed.embed_documents(texts)
        assert len(embeddings) == 20
        # 每个都是一致维度
        for emb in embeddings:
            assert len(emb) == 1024

    def test_retry_on_failure(self):
        """验证重试逻辑：前两次失败，第三次成功"""
        with patch("ollama.Client", autospec=True) as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            call_count = [0]

            def fake_embed_with_retry(model, input, **kwargs):
                call_count[0] += 1
                if call_count[0] < 3:
                    raise ConnectionError("模拟网络错误")
                if isinstance(input, list):
                    return {"embeddings": [[0.1] * 1024 for _ in input]}
                return {"embedding": [0.1] * 1024}

            mock_client.embed.side_effect = fake_embed_with_retry

            from src.embedding.embedder import LawEmbedder
            embedder = LawEmbedder(
                model="bge-m3", base_url="http://localhost:11434",
                max_retries=3, retry_delay=0.01,
            )
            embeddings = embedder.embed_documents(["测试"])
            assert len(embeddings) == 1
            assert call_count[0] == 3  # 第三次成功


def test_embedder_attributes(mock_ollama_embed):
    assert mock_ollama_embed.model == "bge-m3"
    assert mock_ollama_embed.batch_size == 8
    assert mock_ollama_embed.max_retries == 3
