"""
法名推断软信号重排加权（B2 二阶段，docs/B2-法名推断spike报告-2026-08-30.md §6）。

机制：查询向量与法律质心矩阵做最近邻（复用主检索已算的查询向量语义，
此处取一次查询 embedding）→ top3 候选法名 → 检索结果中 law_name（归一化）
命中候选者加性加权后重排。**软信号，绝不硬过滤**——推断错了结果原样保留。

激活门控（与 HybridRetriever 的条件激活同一哲学）：
- 查询含任一已知法名/简称 → 不激活（带法名的查询走精确路径，precise 已
  97.5%，推断信号可能误导）；
- 查询含「第X条」 → 不激活（条号精确查询）；
- 其余（无法名口语查询）→ 激活。

失败语义：质心未就绪/加载失败/任何异常 → 原序返回，主链路不受影响。
成本：无法名查询 +1 次本地 embed（bge-m3）；质心点积微秒级。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np

from src.rag.law_centroids import LawCentroids, norm_law_name
from src.rag.retriever import BaseRetriever

logger = logging.getLogger(__name__)

_ARTICLE_RE = re.compile(r"第[零一二两三四五六七八九十百千\d]+条")


class LawNameBoostRetriever(BaseRetriever):
    """检索器装饰器：质心最近邻法名候选 → 命中结果加性加权重排。

    Args:
        base_retriever: 内层检索器（接入点在 Rerank 之后、Adjacent 之前——
            邻居扩展应跟随加权后的核心排序）
        embedder: 与主检索同一 embedder（同向量空间）
        centroids: LawCentroids 实例（可注入测试替身）
        boost: 加性权重（rerank 分数 0~1 尺度，0.1 ≈ 跨 2~5 名）
        top_laws: 参与加权的候选法名数
    """

    def __init__(
        self,
        base_retriever: BaseRetriever,
        embedder: Any,
        centroids: LawCentroids,
        boost: float = 0.1,
        top_laws: int = 3,
    ):
        self._base = base_retriever
        self._embedder = embedder
        self._centroids = centroids
        self._boost = float(boost)
        self._top_laws = max(1, int(top_laws))

    def is_ready(self) -> bool:
        return self._base.is_ready()

    def search(self, query: str, top_k: int = 5, doc_type: str | None = None) -> list:
        results = self._base.search(query, top_k=top_k, doc_type=doc_type)
        if not results:
            return results
        try:
            results = self._boost_if_applicable(query, results)
        except Exception as e:
            # 加权组件任何故障 → 原序返回（不阻断主链路）
            logger.warning(f"法名加权失败（原序返回）: {e}")
        return results

    def _boost_if_applicable(self, query: str, results: list) -> list:
        # 门控 1：带法名/条号的查询走精确路径，不干预
        if _ARTICLE_RE.search(query or "") or self._centroids.contains_law_name(query or ""):
            return results
        self._centroids.ensure_loaded()
        if not self._centroids.is_ready:
            return results
        qv = np.asarray(self._embedder.embed_query(query), dtype=np.float32)
        candidate_laws = set(self._centroids.top_laws(qv, self._top_laws))
        if not candidate_laws:
            return results
        changed = False
        for doc in results:
            law = getattr(doc, "law_name", "") or (doc.get("law_name", "") if isinstance(doc, dict) else "")
            # score>0 的才是精排真实结果；0 分为邻居填充占位，不参与加权
            score = getattr(doc, "score", 0) or (doc.get("score", 0) if isinstance(doc, dict) else 0)
            if score > 0 and norm_law_name(law) in candidate_laws:
                if isinstance(doc, dict):
                    doc["score"] = round(float(score) + self._boost, 4)
                else:
                    doc.score = round(float(score) + self._boost, 4)
                changed = True
        if changed:
            results.sort(key=lambda d: d.get("score", 0) if isinstance(d, dict) else d.score, reverse=True)
        return results
