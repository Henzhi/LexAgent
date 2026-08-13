"""BM25 关键词检索器（rank-based，不参与分数融合）。

设计要点（对齐"BM25 不要按照分数检索，就按照返回顺序"）:
  - 用 jieba 对 chunks 全文做中文分词，构建 BM25Okapi 索引
  - search() 返回按 BM25 排名排序的 RetrievedDoc 列表
  - score 字段仅填充"排名倒数"作为排序占位，融合层用 RRF（只看排名）而非分数
  - 与向量精确检索完全解耦，只负责关键词召回一路

用法:
  from src.rag.bm25_retriever import Bm25Retriever
  bm25 = Bm25Retriever(store)
  bm25.load_index()          # 首次构建（51348 chunks 约 10-20s）
  docs = bm25.search("刑法第二十条", top_k=10)
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 分词停顿词（法律领域高频但无区分度）
_STOPWORDS = {
    "的", "了", "和", "是", "在", "与", "及", "或", "等", "对", "为",
    "由", "按照", "根据", "依照", "应当", "可以", "不得", "规定", "本条",
    "之", "其", "该", "并", "也", "而", "以及", "但", "本条", "第一款",
    "年月日", "万元", "规定", "中华人民共和国",
}


def _tokenize(text: str) -> list[str]:
    import jieba

    text = re.sub(r"[\s\r\n\t《》（）()【】\[\]\"'“”‘’·,—。；：！？、]+", " ", text or "")
    tokens = []
    for w in jieba.cut(text):
        w = w.strip()
        if not w or w in _STOPWORDS:
            continue
        tokens.append(w)
    return tokens


class Bm25Retriever:
    """Python 端 BM25 检索器（jieba + rank-bm25）。"""

    def __init__(self, store, index_path: Optional[str] = None):
        """
        Args:
            store: PgvectorStore 实例（用于读取 chunks 构建索引）
            index_path: 索引缓存路径（可选，后续可持久化）
        """
        self._store = store
        self._index_path = index_path
        self._bm25 = None
        self._chunks: list[dict] = []  # {content, metadata}
        # 保护懒加载与索引读取（多请求并发时避免重复构建 / 读写竞争）
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def load_index(self, force: bool = False) -> None:
        """从 DB 加载全部 chunks 并构建 BM25 索引。

        关键: chunk content 只有条文正文、不含法名（法名仅在 metadata）。
        向量索引无法靠"法名"匹配，但 BM25 可以——这里把 law_name 拼进
        索引文本（"法名 第X条 正文"），让 BM25 对"法名+关键词"查询有
        精确匹配能力（与向量语义检索形成互补）。
        """
        with self._lock:
            if self._bm25 is not None and not force:
                return
            from rank_bm25 import BM25Okapi

            self._store.ensure_tables()
            rows = self._store.fetch_all_active_chunks()
            self._chunks = [
                {"content": r[0] or "", "metadata": r[1] or {}}
                for r in rows
            ]
            # 法名拼入索引文本（去重，避免重复计数干扰 BM25 词频）
            tokenized = []
            for c in self._chunks:
                meta = c["metadata"]
                law_name = meta.get("law_name", "")
                full_text = f"{law_name} {c['content']}" if law_name else c["content"]
                tokenized.append(_tokenize(full_text))
            self._bm25 = BM25Okapi(tokenized)
            logger.info(f"BM25 索引就绪: {len(self._chunks)} 个 chunks（法名已拼入）")

    def is_ready(self) -> bool:
        return self._bm25 is not None

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 10, **kwargs) -> list:
        from .retriever import RetrievedDoc

        if self._bm25 is None:
            self.load_index()
        with self._lock:
            tokens = _tokenize(query)
            if not tokens:
                return []
            scores = self._bm25.get_scores(tokens)
            # 按 BM25 分数排名取 top_k（排名即顺序；score 仅作排序，不参与融合）
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            docs: list[RetrievedDoc] = []
            for rank, idx in enumerate(order[:top_k], 1):
                meta = self._chunks[idx]["metadata"]
                docs.append(RetrievedDoc(
                    content=self._chunks[idx]["content"],
                    score=1.0 / rank,  # 排名倒数，仅占位
                    law_name=meta.get("law_name", ""),
                    chapter=meta.get("chapter", ""),
                    section=meta.get("section", ""),
                    article_range=meta.get("article_range", ""),
                    chunk_type=meta.get("chunk_type", ""),
                ))
            return docs
