"""
Reranker 二次精排模块。

流程: 粗排(top_k*N) → Reranker 精排 → Top_K

使用 BAAI/bge-reranker-v2-m3 Cross-Encoder,
对中文法律文本重排序效果显著。
"""

from __future__ import annotations

import logging

from sentence_transformers import CrossEncoder

from src.config import RERANK_MAX_CHARS

from .article_router import is_article_routed_query
from .retriever import RetrievedDoc, BaseRetriever

logger = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


class Reranker:
    """Cross-Encoder 精排器"""

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL):
        self.model_name = model_name
        logger.info(f"加载 Reranker: {model_name} ...")
        try:
            # 不传 device → 自动检测 CUDA/CPU
            self._model = CrossEncoder(model_name, local_files_only=True)
            logger.info("Reranker 就绪")
        except Exception as e:
            # 模型不可用（CI 无缓存/无网络、首次部署未预下载等）不致命：
            # 精排是可选的 quality 增益，降级为「跳过精排」继续服务（D-M3-8 同款
            # 原则——辅助组件故障不拖垮主链路）。实例可继续构造，rerank() 短路。
            self._model = None
            logger.warning(f"Reranker 加载失败，已降级为跳过精排（原样返回候选）: {type(e).__name__}: {e}")

    @property
    def available(self) -> bool:
        """模型是否可用（false = 精排被跳过）。"""
        return self._model is not None

    def rerank(
        self,
        query: str,
        docs: list[RetrievedDoc],
        top_k: int = 5,
    ) -> list[RetrievedDoc]:
        """精排候选文档，返回 top_k"""
        if len(docs) <= top_k:
            return docs
        if self._model is None:
            # 模型加载失败：原样返回前 top_k（粗排顺序），不中断检索
            return docs[:top_k]

        # 只截打分输入、不改返回内容：CrossEncoder 耗时对文本长度呈 O(n²)
        # （实测 2000 字 40 对 ~20s），库内 chunk P99=480 字，800 字上限覆盖 99%+
        pairs = [[query, doc.content[:RERANK_MAX_CHARS]] for doc in docs]
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
        # 防呆：recall_k <= top_k 时 rerank() 会短路跳过（len(docs) <= top_k 直接返回），
        # 配置看似启用了精排、实际从未执行（历史坑：15==15 生产长期未生效）
        if recall_k <= top_k:
            logger.warning(
                f"RerankRetriever: recall_k({recall_k}) <= top_k({top_k})，"
                "rerank 将被静默跳过；如需启用精排请保证 RERANK_RECALL_K > RERANK_TOP_K"
            )

    def search(self, query: str, top_k: int = 5, **kwargs) -> list[RetrievedDoc]:
        effective_k = top_k or self._top_k
        # 分级召回（P1b）：「法名+第X条」类查询由 ArticleRouter 精确置顶，
        # rerank 对其召回无增益，跳过可省 ~1.3s，把精排成本留给模糊查询
        if is_article_routed_query(query):
            return self._base.search(query, top_k=effective_k, **kwargs)
        candidates = self._base.search(query, top_k=self._recall_k, **kwargs)
        return self._reranker.rerank(query, candidates, top_k=effective_k)

    def is_ready(self) -> bool:
        return self._base.is_ready()
