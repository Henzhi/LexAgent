"""条款号精确路由检索器（装饰器模式）。

问题背景:
  按「第X条」重切分后，chunk content 只含条文正文、不含法名（法名仅在
  metadata 中）。纯向量检索对"法名+条款号"类查询（如"刑法第二十条"）
  会被条款号干扰，召回其他法律的同号条文，目标条文丢失。

方案:
  对含"第X条"的查询，规则解析 (法名线索, 条款号)，从 DB 精确定位该条文
  chunk（metadata.law_name 模糊匹配 + article_range 精确匹配），置顶注入
  检索结果。解析失败则原样透传基础检索结果，不影响既有行为。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .retriever import BaseRetriever, RetrievedDoc

logger = logging.getLogger(__name__)

_ARTICLE_RE = re.compile(r"第([零一二两三四五六七八九十百千]+)条")
_BOOK_RE = re.compile(r"《([^》]+)》")
# 法律名线索：以"法/条例/规定/办法/细则/规则/公约/决定/章程/标准"结尾的片段
_LAW_HINT_RE = re.compile(r"([\u4e00-\u9fff]{1,24}?(?:法|条例|规定|办法|细则|规则|公约|决定|章程|标准|通则))")

_CN = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000,
}


def _cn2int(cn: str) -> int:
    result, unit = 0, 1
    i = len(cn) - 1
    while i >= 0:
        val = _CN.get(cn[i], 0)
        if val >= 10:
            unit = val
            if i == 0:
                result += unit
            i -= 1
            continue
        result += val * unit
        unit = 1
        i -= 1
    return result


def _range_contains(article_range: str, num: int) -> bool:
    """article_range（如'第十九条至第二十条'）是否包含目标条款号"""
    nums: set[int] = set()
    for m in re.finditer(r"第([零一二两三四五六七八九十百千]+)条", article_range or ""):
        a = _cn2int(m.group(1))
        nums.add(a)
        # 简单区间展开：'第X条至第Y条'
        m2 = re.search(
            re.escape(m.group(0)) + r"\s*(?:至|到)\s*第([零一二两三四五六七八九十百千]+)条",
            article_range or "",
        )
        if m2:
            b = _cn2int(m2.group(1))
            nums.update(range(a, b + 1))
    return num in nums


class ArticleRouter(BaseRetriever):
    """条款号精确路由：把含'第X条'的查询精确命中的条文置顶。"""

    def __init__(self, base_retriever: BaseRetriever, store):
        """
        Args:
            base_retriever: 基础检索器（pgvector / Reranker / Adjacent 装饰链）
            store: PgvectorStore 实例（用于精确查库）
        """
        self._base = base_retriever
        self._store = store

    def search(self, query: str, top_k: int = 5, **kwargs) -> list[RetrievedDoc]:
        routed = self._locate(query)
        if not routed:
            return self._base.search(query, top_k=top_k, **kwargs)

        # 精确命中置顶，再拼基础检索结果（去重）
        results = self._base.search(query, top_k=top_k, **kwargs)
        seen = {(r.law_name, r.article_range) for r in routed}
        merged = list(routed)
        for r in results:
            key = (r.law_name, r.article_range)
            if key not in seen:
                seen.add(key)
                merged.append(r)
        return merged[:top_k]

    def is_ready(self) -> bool:
        return self._base.is_ready()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _locate(self, query: str) -> list[RetrievedDoc]:
        """尝试精确路由；无'第X条'或无法确定法名时返回空列表。"""
        m = _ARTICLE_RE.search(query)
        if not m:
            return []
        num = _cn2int(m.group(1))
        law_hint = self._extract_law_hint(query)
        if not law_hint:
            return []
        return self._query_db(law_hint, num, limit=4)

    def _extract_law_hint(self, query: str) -> Optional[str]:
        """提取法名线索：优先《书名号》，否则取法律类后缀词。"""
        mb = _BOOK_RE.search(query)
        if mb:
            return mb.group(1).strip()
        # 去掉条款号片段后，再匹配法律后缀词，避免把"第十条"一起吞掉
        cleaned = _ARTICLE_RE.sub("", query)
        mh = _LAW_HINT_RE.search(cleaned)
        if mh:
            return mh.group(1).strip()
        return None

    def _query_db(self, law_hint: str, num: int, limit: int) -> list[RetrievedDoc]:
        """按法名模糊 + 条款号精确查库。

        使用 store 的锁保护共享 PG 连接（避免与 store 自身方法并发竞争）。
        """
        try:
            with self._store._lock:
                self._store._ensure_connection()
                with self._store._conn.cursor() as cur:
                    cur.execute(
                        "SELECT dc.content, dc.metadata, "
                        "1 - (dc.embedding <=> dc.embedding) AS score "
                        "FROM document_chunks dc "
                        "JOIN documents d ON dc.doc_id = d.id "
                        "WHERE d.status = 'active' "
                        "AND dc.metadata->>'law_name' LIKE %s "
                        "AND dc.chunk_type = 'article' "
                        "LIMIT 500",
                        (f"%{law_hint}%",),
                    )
                    rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"article_router 查库失败: {e}")
            return []

        found: list[RetrievedDoc] = []
        for content, meta, _score in rows:
            meta = meta or {}
            if not _range_contains(meta.get("article_range", ""), num):
                continue
            found.append(RetrievedDoc(
                content=content,
                score=0.99,
                law_name=meta.get("law_name", ""),
                chapter=meta.get("chapter", ""),
                section=meta.get("section", ""),
                article_range=meta.get("article_range", ""),
                chunk_type=meta.get("chunk_type", ""),
            ))
            if len(found) >= limit:
                break
        if found:
            logger.info(f"article_router 精确命中: {law_hint} 第{num}条 → {len(found)} 条")
        return found
