"""
基于 Ollama 的文本向量化模块。

直接调用 Ollama REST API（通过 ollama 包），实现 LangChain 兼容的
Embeddings 接口，避免 langchain-ollama 的版本冲突问题。

特性：
  - 自动批处理，避免逐条调用
  - 本地运行，零网络依赖
  - 实现 langchain_core.embeddings.Embeddings 接口，可直接用于向量库
"""
from __future__ import annotations

import time
import logging
from typing import List

import ollama
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)


class LawEmbedder(Embeddings):
    """法律文档向量化器

    直接通过 ollama SDK 调用 Ollama 服务，实现 LangChain Embeddings 接口，
    使其可直接用于 LangChain 兼容的向量库（pgvector 等）。
    """

    def __init__(
        self,
        model: str = 'bge-m3',
        base_url: str = 'http://localhost:11434',
        batch_size: int = 32,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        """
        Args:
            model: Ollama embedding 模型名称（需已通过 ollama pull 下载）
            base_url: Ollama 服务地址
            batch_size: 每批 embedding 的文本数量
            max_retries: 调用失败时最大重试次数
            retry_delay: 重试间隔（秒）
        """
        self.model = model
        self.base_url = base_url
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # 创建 ollama 客户端
        host = base_url.replace('http://', '').replace('https://', '')
        if ':' in host:
            host, port = host.rsplit(':', 1)
        else:
            host, port = host, '11434'

        self._client = ollama.Client(host=f'http://{host}:{port}')

    # ------------------------------------------------------------------
    # LangChain Embeddings 接口
    # ------------------------------------------------------------------

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """对一批文本生成向量（LangChain 标准接口）

        Args:
            texts: 文本列表

        Returns:
            list[list[float]]: 与 texts 等长的向量列表
        """
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        total = len(texts)

        for i in range(0, total, self.batch_size):
            batch = texts[i:i + self.batch_size]
            batch_embs = self._embed_batch(batch)
            all_embeddings.extend(batch_embs)
        return all_embeddings

    def embed_query(self, text: str) -> List[float]:
        """对单条查询文本生成向量（LangChain 标准接口）"""
        result = self._embed_batch([text])
        return result[0]

    # ------------------------------------------------------------------
    # 自定义方法
    # ------------------------------------------------------------------

    def embed_documents_with_progress(
        self,
        texts: List[str],
        show_progress: bool = True,
    ) -> List[List[float]]:
        """带进度显示的批量向量化

        Args:
            texts: 文本列表
            show_progress: 是否打印进度

        Returns:
            list[list[float]]
        """
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        total = len(texts)

        for i in range(0, total, self.batch_size):
            batch = texts[i:i + self.batch_size]
            batch_embs = self._embed_batch(batch)
            all_embeddings.extend(batch_embs)

            if show_progress:
                done = min(i + self.batch_size, total)
                logger.info(f'Embedding 进度: {done}/{total}')

        return all_embeddings

    def get_embedding_dim(self) -> int:
        """获取当前模型的向量维度"""
        test_vec = self.embed_query('test')
        return len(test_vec)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        """对一批文本调用 Ollama embedding API（带重试）"""
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.embed(
                    model=self.model,
                    input=texts,
                )
                return response['embeddings']
            except Exception as e:
                last_error = e
                logger.warning(
                    f'Embedding 调用失败 (尝试 {attempt}/{self.max_retries}): {e}'
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)

        raise RuntimeError(
            f'Embedding 调用失败，已重试 {self.max_retries} 次: {last_error}'
        )
