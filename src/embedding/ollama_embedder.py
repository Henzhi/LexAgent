"""
Ollama Embedding 后端实现。

通过 ollama Python SDK 调用本地 Ollama 服务的 Embedding API。
实现 LangChain Embeddings 兼容接口，可直接用于 LangChain 兼容的向量库。
"""
from __future__ import annotations

import logging
from typing import List

import ollama
from langchain_core.embeddings import Embeddings

from src.embedding.base import EmbeddingBackend

logger = logging.getLogger(__name__)


class OllamaEmbedder(EmbeddingBackend):
    """Ollama Embedding 后端

    用法:
        embedder = OllamaEmbedder(model="bge-m3", base_url="http://localhost:11434")
        vec = embedder.embed_query("中华人民共和国刑法")
        vecs = embedder.embed(["文本1", "文本2"])
    """

    def __init__(
        self,
        model: str = "bge-m3",
        base_url: str = "http://localhost:11434",
        batch_size: int = 32,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        super().__init__(
            model=model,
            batch_size=batch_size,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        self.base_url = base_url
        self._client = self._init_client()
        self._cached_dimension: int | None = None

    def _init_client(self) -> ollama.Client:
        host = self.base_url.replace("http://", "").replace("https://", "")
        return ollama.Client(host=host, timeout=300.0)

    def get_model_name(self) -> str:
        return f"ollama:{self.model}"

    def get_dimension(self) -> int:
        if self._cached_dimension is None:
            # 已知模型的维度表，避免不必要的 API 调用
            _KNOWN_DIMS = {
                "bge-m3": 1024,
                "nomic-embed-text": 768,
                "bge-large": 1024,
                "mxbai-embed-large": 1024,
            }
            known = _KNOWN_DIMS.get(self.model)
            if known:
                self._cached_dimension = known
            else:
                self._cached_dimension = len(self.embed_query("test"))
        return self._cached_dimension

    # ------------------------------------------------------------------
    # EmbeddingBackend 抽象方法实现
    # ------------------------------------------------------------------

    def _embed_batch_impl(self, texts: List[str]) -> List[List[float]]:
        response = self._client.embed(
            model=self.model,
            input=texts,
        )
        return response["embeddings"]


class OllamaLangChainEmbedder(Embeddings):
    """将 OllamaEmbedder 包装为 LangChain Embeddings 接口

    使 Ollama Embedding 后端可以无缝用于 LangChain 兼容的向量库。
    """

    def __init__(self, backend: OllamaEmbedder):
        self._backend = backend

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._backend.embed(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._backend.embed_query(text)
