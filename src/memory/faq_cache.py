"""
FAQ 语义缓存管理器

将高频法律问答缓存到 pgvector，语义相似度 > 0.95 时直接返回。
节省 LLM 调用成本，降低响应延迟（缓存命中时 <100ms）。

设计要点:
  - 写入: 仅当 RAG 回答通过校验（conf > 0.8）时才缓存
  - 命中: 余弦相似度 > 0.95 且 status='active' 且未过期
  - 失效: 法律修订时级联标记 related_laws 列表中所有缓存为 invalidated
  - 清理: 定时任务删除过期 / 低频缓存
  - 连接（2026-09-03 连接池整改）：每次操作从进程级池借用，无常驻连接/锁

用法:
    cache = FAQCache(PG_CONN, embedder)
    hit = cache.check(query)      # None = 未命中
    cache.store(query, answer, sources, related_laws)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# 命中阈值：余弦相似度 >= 此值时视为命中
HIT_THRESHOLD = 0.95

# TTL：默认 1 小时（超过即销毁；命中会刷新续期）。可通过 FAQ_TTL_HOURS 覆盖
try:
    from src.config import FAQ_TTL_HOURS as _FAQ_TTL_HOURS

    DEFAULT_TTL_HOURS = _FAQ_TTL_HOURS
except Exception:
    DEFAULT_TTL_HOURS = 1


class FAQCache:
    """FAQ 语义缓存管理器（连接池借用）。

    Attributes:
        _embedder: EmbeddingAdapter（用于查询向量化）
    """

    def __init__(self, conn_string: str, embedder):
        self._embedder = embedder
        # conn_string 保留为兼容签名；实际连接统一走进程级连接池
        self._conn_string = conn_string

    def close(self):
        """兼容 no-op：连接由池管理，无常驻连接可关。"""
        return None

    # ------------------------------------------------------------------
    # 缓存查询
    # ------------------------------------------------------------------

    def check(self, query: str) -> dict | None:
        """检查是否有缓存命中

        Args:
            query: 用户原始问题

        Returns:
            命中时返回 {"answer", "sources", "score"}，未命中返回 None
        """
        from src.db.pool import db_connection

        vec = self._embedder.embed_query(query)

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, answer, sources, "
                    "1 - (question_embed <=> %s::halfvec) AS score "
                    "FROM faq_cache "
                    "WHERE status = 'active' "
                    "  AND expires_at > NOW() "
                    "  AND 1 - (question_embed <=> %s::halfvec) >= %s "
                    "ORDER BY question_embed <=> %s::halfvec "
                    "LIMIT 1",
                    (vec, vec, HIT_THRESHOLD, vec),
                )
                row = cur.fetchone()

            if row is None:
                return None

            faq_id, answer, sources_raw, score = row

            # 解析 JSON — PSQL 中 sources 以 json.dumps 写入，取回后是 str
            import json as _json

            try:
                sources = _json.loads(sources_raw) if isinstance(sources_raw, str) else (sources_raw or [])
            except (_json.JSONDecodeError, TypeError):
                sources = []

            # 命中即续期：hit_count + 1，同时把 TTL 顺延刷新
            # （热问题自动续命，冷问题自然过期销毁）
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE faq_cache SET hit_count = hit_count + 1, "
                    "expires_at = NOW() + INTERVAL '%s hours' "
                    "WHERE id = %s",
                    (DEFAULT_TTL_HOURS, faq_id),
                )
            conn.commit()

        logger.info(f"FAQ缓存命中: score={round(float(score), 4)}")
        return {
            "answer": answer,
            "sources": sources,
            "score": round(float(score), 4),
        }

    # ------------------------------------------------------------------
    # 缓存写入
    # ------------------------------------------------------------------

    def store(
        self,
        question: str,
        answer: str,
        sources: list[dict] | None = None,
        related_laws: list[str] | None = None,
        confidence: float = 1.0,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ):
        """写入缓存

        Args:
            question: 用户原始问题
            answer: 完整回答
            sources: 引用来源
            related_laws: 关联法律 ID 列表
            confidence: 回答置信度（<0.8 不缓存）
            ttl_hours: 过期小时数（默认 1，命中后刷新续期）
        """
        if confidence < 0.8:
            return

        from src.db.pool import db_connection

        vec = self._embedder.embed_query(question)

        import json

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO faq_cache "
                    "(question, question_embed, answer, sources, related_laws, "
                    " confidence, hit_count, status, expires_at) "
                    "VALUES (%s, %s::halfvec, %s, %s, %s, %s, 1, 'active', "
                    " NOW() + INTERVAL '%s hours')",
                    (
                        question,
                        vec,
                        answer,
                        json.dumps(sources or [], ensure_ascii=False),
                        related_laws or [],
                        confidence,
                        ttl_hours,
                    ),
                )
            conn.commit()
        logger.info(f"FAQ缓存写入: '{question[:40]}...'")

    # ------------------------------------------------------------------
    # 缓存失效
    # ------------------------------------------------------------------

    def invalidate_by_law(self, law_id: str) -> int:
        """法律修订时，级联失效所有引用该法律的缓存

        Returns:
            失效的缓存条数
        """
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE faq_cache SET status = 'invalidated' WHERE status = 'active' AND %s = ANY(related_laws)",
                    (law_id,),
                )
                count = cur.rowcount
            conn.commit()
        logger.warning(f"FAQ缓存级联失效: law={law_id}, {count}条")
        return count

    def clean_expired(self) -> int:
        """清理过期缓存"""
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM faq_cache WHERE expires_at < NOW()")
                count = cur.rowcount
            conn.commit()
        if count > 0:
            logger.info(f"FAQ缓存清理: {count}条过期")
        return count
