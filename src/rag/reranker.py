"""
Reranker 二次精排模块。

流程: 粗排(top_k*N) → Reranker 精排 → Top_K

使用 BAAI/bge-reranker-v2-m3 Cross-Encoder,
对中文法律文本重排序效果显著。
"""
from __future__ import annotations

import logging

from sentence_transformers import CrossEncoder

from .retriever import RetrievedDoc, BaseRetriever

logger = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


class Reranker:
    """Cross-Encoder 精排器"""

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL):
        self.model_name = model_name
        logger.info(f"加载 Reranker: {model_name} ...")
        # 不传 device → 自动检测 CUDA/CPU
        self._model = CrossEncoder(model_name, local_files_only=True)
        logger.info("Reranker 就绪")

    def rerank(
        self, query: str, docs: list[RetrievedDoc], top_k: int = 5,
    ) -> list[RetrievedDoc]:
        """精排候选文档，返回 top_k"""
        if len(docs) <= top_k:
            return docs

        pairs = [[query, doc.content] for doc in docs]
        scores = self._model.predict(pairs, show_progress_bar=False)

        scored = list(zip(docs, scores))
        scored.sort(key=lambda x: x[1], reverse=True)

        result = []
        for doc, score in scored[:top_k]:
            doc.score = round(float(score), 4)
            result.append(doc)
        return result


class RerankRetriever(BaseRetriever):
    """带 Reranker 的检索器装饰器"""

    def __init__(
        self,
        base_retriever: BaseRetriever,
        reranker: Reranker,
        recall_k: int = 20,
        top_k: int = 5,
    ):
        self._base = base_retriever
        self._reranker = reranker
        self._recall_k = recall_k
        self._top_k = top_k

    def search(self, query: str, top_k: int = 5, **kwargs) -> list[RetrievedDoc]:
        candidates = self._base.search(query, top_k=self._recall_k, **kwargs)
        return self._reranker.rerank(query, candidates, top_k=top_k or self._top_k)

    def is_ready(self) -> bool:
        return self._base.is_ready()
