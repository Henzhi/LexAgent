"""条件激活的 rank-based 混合检索器。

设计要点（对齐"BM25 不要按照分数检索，就按照返回顺序"）:
  - 向量精确检索（base）按"向量分数"排序，是主路径，覆盖通用语义查询
  - BM25 按"返回顺序"（排名）参与，只在查询**含明确法律实体**（法名/条款号）
    时激活，作为关键词精确补充——与向量语义检索形成互补
  - 激活时用加权 RRF 融合：fusion = Σ w_i/(k + rank_i)，只看排名不碰分数
  - 未激活时纯向量返回，BM25 零开销零干扰

法名/条款号识别（_should_activate）:
  - 含"第X条"（如"刑法第二十条"）
  - 含《书名号》法名（如《民法典》）
  - 含"X法/X条例/X规定/X办法/X细则/X规则"等法律后缀词
"""
from __future__ import annotations

import logging
import re

from .retriever import BaseRetriever

logger = logging.getLogger(__name__)

_ARTICLE_RE = re.compile(r"第[零一二两三四五六七八九十百千]+条")
_BOOK_RE = re.compile(r"《[^》]+》")
_LAW_SUFFIX_RE = re.compile(
    r"[\u4e00-\u9fff]{1,24}?(?:法|条例|规定|办法|细则|规则|公约|决定|章程|标准|通则)"
)


class HybridRetriever(BaseRetriever):
    """向量 + BM25 的条件激活 rank-based 混合检索器。"""

    def __init__(
        self,
        base_retriever: BaseRetriever,
        bm25_retriever,
        rrf_k: int = 60,
        vector_top_n: int = 15,
        bm25_top_n: int = 15,
        vec_weight: float = 1.0,
        bm25_weight: float = 0.5,
    ):
        """
        Args:
            base_retriever: 向量精确检索装饰链（pgvector → rerank → adjacent）
            bm25_retriever: Bm25Retriever 实例
            rrf_k: RRF 常数（标准值 60）
            vector_top_n: 向量一路取多少条参与融合
            bm25_top_n: BM25 一路取多少条参与融合
            vec_weight: 向量一路融合权重（默认 1.0）
            bm25_weight: BM25 一路融合权重（激活时默认 0.5）
        """
        self._base = base_retriever
        self._bm25 = bm25_retriever
        self._rrf_k = rrf_k
        self._vector_top_n = vector_top_n
        self._bm25_top_n = bm25_top_n
        self._vec_weight = vec_weight
        self._bm25_weight = bm25_weight

    def is_ready(self) -> bool:
        return self._base.is_ready()

    def search(self, query: str, top_k: int = 5, **kwargs) -> list:
        # 条件激活：只有查询含明确法律实体时 BM25 才参与
        if self._should_activate(query):
            return self._hybrid_search(query, top_k=top_k, **kwargs)
        return self._base.search(query, top_k=top_k, **kwargs)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _should_activate(self, query: str) -> bool:
        """查询是否含明确法律实体（法名/条款号）。"""
        if _ARTICLE_RE.search(query):
            return True
        if _BOOK_RE.search(query):
            return True
        if _LAW_SUFFIX_RE.search(query):
            return True
        return False

    def _hybrid_search(self, query: str, top_k: int = 5, **kwargs) -> list:
        # 两路并行召回（BM25 索引懒加载）
        vec_docs = self._base.search(query, top_k=self._vector_top_n, **kwargs)
        bm_docs = self._bm25.search(query, top_k=self._bm25_top_n)

        # 按 (law_name, article_range) 去重聚合各路的排名
        fusion: dict[tuple, dict] = {}
        for rank, d in enumerate(vec_docs, 1):
            key = (d.law_name, d.article_range)
            fusion.setdefault(key, {"doc": d, "vec": None, "bm": None})
            fusion[key]["vec"] = rank
        for rank, d in enumerate(bm_docs, 1):
            key = (d.law_name, d.article_range)
            entry = fusion.setdefault(key, {"doc": d, "vec": None, "bm": None})
            entry["bm"] = rank

        # 加权 RRF：只按排名算融合分（不碰分数），BM25 弱权重仅作补充
        ranked = []
        for key, e in fusion.items():
            score = 0.0
            if e["vec"] is not None:
                score += self._vec_weight / (self._rrf_k + e["vec"])
            if e["bm"] is not None:
                score += self._bm25_weight / (self._rrf_k + e["bm"])
            ranked.append((score, e["doc"]))
        ranked.sort(key=lambda x: -x[0])
        return [d for _, d in ranked[:top_k]]
