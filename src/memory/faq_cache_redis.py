"""
FAQ 语义缓存管理器 (Redis Stack 后端, v0.7)

将高频法律问答缓存到 Redis Stack，基于向量检索语义命中直接返回。
相比 pgvector 后端 (faq_cache.py)：
  - 原生 TTL 自动过期（命中 EXPIRE 续期），无需后台清理任务
  - 内存命中延迟更低
  - 修法级联失效：用 Set 记录「法律 → 缓存条目」映射，失效时批量删除

接口与 FAQCache（pgvector 版）完全兼容：check / store / invalidate_by_law /
clean_expired / close，可通过 FAQ_CACHE_BACKEND 切换，pgvector 版作为回退。

用法:
    cache = FAQCacheRedis(REDIS_URL, embedder)
    cache.ensure_index()
    hit = cache.check(query)      # None = 未命中
    cache.store(query, answer, sources, related_laws)
    cache.invalidate_by_law(law_id)
"""
from __future__ import annotations

import json
import logging
import struct
import uuid

import redis

from src.config import FAQ_TTL_HOURS as _FAQ_TTL_HOURS

logger = logging.getLogger(__name__)

# 命中阈值：余弦相似度 >= 此值视为命中（与 pgvector 后端一致）
HIT_THRESHOLD = 0.95

# 默认 TTL（秒）：命中时自动顺延刷新（热问题续命）
DEFAULT_TTL_SECONDS = max(1, _FAQ_TTL_HOURS) * 3600

# Redis key 前缀
_FAQ_KEY_PREFIX = "faq:"
_LAW_INDEX_PREFIX = "faq:law:"   # Set: 法律ID → 关联缓存条目ID
_INDEX_NAME = "idx:faq"


class FAQCacheRedis:
    """FAQ 语义缓存（Redis Stack 后端）

    Attributes:
        _client: redis.Redis 实例（redis-py 客户端线程安全，无需加锁）
        _embedder: EmbeddingAdapter（用于问题向量化）
        _dim: 向量维度（ensure_index 时使用）
    """

    def __init__(self, redis_url: str, embedder, dim: int | None = None):
        self._embedder = embedder
        self._url = redis_url
        # decode_responses=True：FT.SEARCH 返回的文本字段自动解码；
        # question_embed 存二进制 bytes，不参与文本解码
        self._client = redis.Redis.from_url(redis_url, decode_responses=True)
        # 向量维度：优先显式传入，否则从 embedder 推断
        if dim is None and embedder is not None:
            get_dim = getattr(embedder, "get_embedding_dim", None)
            dim = get_dim() if callable(get_dim) else None
        self._dim = dim

    # ------------------------------------------------------------------
    # 索引管理
    # ------------------------------------------------------------------

    def ensure_index(self) -> None:
        """创建 HNSW 向量索引（已存在则跳过）"""
        if self._dim is None:
            raise ValueError(
                "无法确定向量维度，请传入 dim 参数或提供带 get_embedding_dim 的 embedder"
            )
        try:
            from redis.commands.search.field import VectorField
            from redis.commands.search.index_definition import (
                IndexDefinition,
                IndexType,
            )

            schema = (
                VectorField(
                    "question_embed",
                    "HNSW",
                    {
                        "TYPE": "FLOAT32",
                        "DIM": self._dim,
                        "DISTANCE_METRIC": "COSINE",
                    },
                ),
            )
            self._client.ft(_INDEX_NAME).create_index(
                schema,
                definition=IndexDefinition(
                    prefix=[_FAQ_KEY_PREFIX], index_type=IndexType.HASH
                ),
            )
            logger.info(f"FAQ 向量索引就绪: {_INDEX_NAME} (dim={self._dim})")
        except redis.ResponseError as e:
            if "already exists" in str(e).lower():
                logger.debug("FAQ 向量索引已存在")
            else:
                raise

    # ------------------------------------------------------------------
    # 编码
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_vector(vec: list[float]) -> bytes:
        """float 列表 → float32 小端 bytes（Redis VecSim 要求）"""
        return struct.pack(f"<{len(vec)}f", *vec)

    # ------------------------------------------------------------------
    # 缓存查询
    # ------------------------------------------------------------------

    def check(self, query: str) -> dict | None:
        """检查是否有缓存命中（语义相似度 >= 0.95）

        命中即续期 TTL + hit_count 递增（热问题自动续命，冷问题自然过期销毁）。

        Returns:
            命中时 {"answer", "sources", "score"}，未命中返回 None
        """
        if self._embedder is None:
            return None
        vec = self._embedder.embed_query(query)
        try:
            from redis.commands.search.query import Query

            # redis-py 8.x：向量参数通过 search(query, query_params=...) 传入
            res = self._client.ft(_INDEX_NAME).search(
                Query("*=>[KNN 1 @question_embed $vec AS score]")
                .sort_by("score")
                .return_fields(
                    "id", "question", "answer", "sources",
                    "related_laws", "confidence", "hit_count", "score",
                )
                .dialect(2),
                query_params={"vec": self._pack_vector(vec)},
            )
        except redis.ResponseError as e:
            # 索引不存在（Redis Stack 未就绪/未建索引）→ 视为未命中
            logger.warning(f"FAQ 向量检索失败（索引未就绪？）: {e}")
            return None

        if not res.docs:
            return None

        doc = res.docs[0]
        distance = float(getattr(doc, "score", 1.0))
        score = 1.0 - distance  # COSINE 距离 → 相似度
        if score < HIT_THRESHOLD:
            return None

        # FT.SEARCH 返回的 doc.id 是完整 key（含 faq: 前缀），直接复用；
        # 防御性处理：极少数情况返回短 id 时再补前缀，避免产生 faq:faq:... 错误 key
        key = doc.id if doc.id.startswith(_FAQ_KEY_PREFIX) else _FAQ_KEY_PREFIX + doc.id

        # 命中续期 + 计数（作用于正确 key，TTL 才会真正刷新）
        self._client.expire(key, DEFAULT_TTL_SECONDS)
        self._client.hincrby(key, "hit_count", 1)

        # 同步续期失效索引 Set：FAQ 持续命中期间，其失效索引必须保持存活，
        # 否则 Set 提前过期会导致修法时找不到这条仍在生效的缓存
        try:
            related = json.loads(doc.related_laws) if isinstance(doc.related_laws, str) else (doc.related_laws or [])
        except (json.JSONDecodeError, TypeError):
            related = []
        for law in related:
            self._client.expire(_LAW_INDEX_PREFIX + str(law), DEFAULT_TTL_SECONDS)

        # 解析 sources（FT 返回 str；防御性处理）
        sources = getattr(doc, "sources", "[]")
        try:
            sources = json.loads(sources) if isinstance(sources, str) else (sources or [])
        except (json.JSONDecodeError, TypeError):
            sources = []

        logger.info(f"FAQ缓存命中(Redis): score={round(score, 4)}")
        return {
            "answer": getattr(doc, "answer", ""),
            "sources": sources,
            "score": round(score, 4),
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
        ttl_hours: int | None = None,
    ):
        """写入缓存（低置信度不写入）

        Args:
            question: 用户原始问题
            answer: 完整回答
            sources: 引用来源
            related_laws: 关联法律 ID 列表（修法时级联失效）
            confidence: 回答置信度（<0.8 不缓存）
            ttl_hours: 过期小时数（默认 1，命中后刷新续期）
        """
        if confidence < 0.8:
            return
        if self._embedder is None:
            return

        vec = self._embedder.embed_query(question)
        related = list(related_laws or [])
        faq_id = uuid.uuid4().hex
        key = _FAQ_KEY_PREFIX + faq_id
        ttl = int(ttl_hours * 3600) if ttl_hours else DEFAULT_TTL_SECONDS

        self._client.hset(
            key,
            mapping={
                "question": question,
                "question_embed": self._pack_vector(vec),
                "answer": answer,
                "sources": json.dumps(sources or [], ensure_ascii=False),
                "related_laws": json.dumps(related, ensure_ascii=False),
                "confidence": confidence,
                "hit_count": 1,
            },
        )
        self._client.expire(key, ttl)
        # 维护「法律 → 缓存条目」Set，供修法级联失效；
        # Set 与 FAQ 同生命周期（同 TTL），避免 FAQ 过期后留下永不过期的僵尸引用
        for law in related:
            law_key = _LAW_INDEX_PREFIX + str(law)
            self._client.sadd(law_key, faq_id)
            self._client.expire(law_key, ttl)
        logger.info(f"FAQ缓存写入(Redis): '{question[:40]}...'")

    # ------------------------------------------------------------------
    # 缓存失效
    # ------------------------------------------------------------------

    def invalidate_by_law(self, law_id: str) -> int:
        """法律修订时，级联删除所有引用该法律的缓存

        Returns:
            失效的缓存条数
        """
        set_key = _LAW_INDEX_PREFIX + str(law_id)
        ids = self._client.smembers(set_key)
        count = len(ids)
        if count:
            pipe = self._client.pipeline()
            for faq_id in ids:
                pipe.delete(_FAQ_KEY_PREFIX + str(faq_id))
            pipe.delete(set_key)
            pipe.execute()
            logger.warning(f"FAQ缓存级联失效(Redis): law={law_id}, {count}条")
        return count

    def clean_expired(self) -> int:
        """清理过期缓存 — Redis 原生 TTL 自动过期，无需主动清理。

        保留此接口仅为与 pgvector 后端兼容（后台清理任务统一调用）。
        """
        return 0

    def close(self):
        """关闭连接"""
        try:
            self._client.close()
        except Exception:
            pass
