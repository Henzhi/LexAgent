"""
OpenAI 兼容 API Embedding 后端实现。

支持所有兼容 OpenAI Embeddings API 的服务:
  - OpenAI (text-embedding-3-small, text-embedding-3-large)
  - 本地 vLLM / Ollama OpenAI 兼容端点
  - 其他兼容服务
"""
from __future__ import annotations

import logging
from typing import List

from openai import OpenAI

from src.embedding.base import EmbeddingBackend

logger = logging.getLogger(__name__)

# 已知模型的向量维度
_OPENAI_EMBED_DIMENSIONS = {
    "text-embedding-3-small":   1536,
    "text-embedding-3-large":   3072,
    "text-embedding-ada-002":   1536,
    "bge-m3":                   1024,
}


class OpenAIEmbedder(EmbeddingBackend):
    """OpenAI 兼容 API Embedding 后端

    用法:
        embedder = OpenAIEmbedder(
            model="text-embedding-3-small",
            api_key="sk-xxx",
        )
        vec = embedder.embed_query("中华人民共和国刑法")
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
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
        self.api_key = api_key
        self.base_url = base_url
        self._client = self._init_client()
        self._cached_dimension: int | None = None

    def _init_client(self) -> OpenAI:
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=0,
            timeout=120.0,
        )

    def get_model_name(self) -> str:
        return f"openai:{self.model}"

    def get_dimension(self) -> int:
        if self._cached_dimension is None:
            # 先查已知维度表，否则调用一次 API
            known = _OPENAI_EMBED_DIMENSIONS.get(self.model)
            if known:
                self._cached_dimension = known
            else:
                self._cached_dimension = len(self.embed_query("test"))
        return self._cached_dimension

    # ------------------------------------------------------------------
    # EmbeddingBackend 抽象方法实现
    # ------------------------------------------------------------------

    def _embed_batch_impl(self, texts: List[str]) -> List[List[float]]:
        response = self._client.embeddings.create(
            model=self.model,
            input=texts,
        )
        # 按 input 顺序返回
        return [item.embedding for item in response.data]
