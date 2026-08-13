"""
FAQ 缓存 Redis Stack 后端单元测试。

验证:
  1. 接口与 pgvector 版 FAQCache 一致（check/store/invalidate_by_law/clean_expired/close）
  2. ensure_index 维度推断与幂等
  3. check 命中/未命中/续期逻辑
  4. store 写入与法律索引
  5. invalidate_by_law 级联删除
  6. clean_expired 为 no-op（TTL 自动过期）
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import redis


def _make_cache(**kwargs):
    """构造 FAQCacheRedis，注入 mock redis client"""
    from src.memory.faq_cache_redis import FAQCacheRedis

    fake_client = MagicMock()
    with patch(
        "src.memory.faq_cache_redis.redis.Redis.from_url",
        return_value=fake_client,
    ):
        cache = FAQCacheRedis("redis://localhost:6379", MagicMock(), **kwargs)
    cache._client = fake_client
    return cache, fake_client


class TestInterface:
    """验证与 pgvector 后端接口一致"""

    def test_same_public_methods(self):
        from src.memory.faq_cache import FAQCache
        from src.memory.faq_cache_redis import FAQCacheRedis

        pg_methods = {m for m in dir(FAQCache) if not m.startswith("_")}
        redis_methods = {m for m in dir(FAQCacheRedis) if not m.startswith("_")}
        # Redis 后端至少覆盖 pg 后端的全部公开接口
        assert {"check", "store", "invalidate_by_law", "clean_expired", "close"} <= redis_methods
        assert "clean_expired" in pg_methods

    def test_hit_threshold_shared(self):
        from src.memory.faq_cache import HIT_THRESHOLD as pg_th
        from src.memory.faq_cache_redis import HIT_THRESHOLD as redis_th
        assert pg_th == redis_th == 0.95


class TestEnsureIndex:
    def test_dim_inferred_from_embedder(self):
        from src.memory.faq_cache_redis import FAQCacheRedis

        embedder = MagicMock()
        embedder.get_embedding_dim.return_value = 1024
        with patch(
            "src.memory.faq_cache_redis.redis.Redis.from_url",
            return_value=MagicMock(),
        ):
            cache = FAQCacheRedis("redis://localhost:6379", embedder)
        assert cache._dim == 1024

    def test_dim_requires_value(self):
        import pytest
        from src.memory.faq_cache_redis import FAQCacheRedis

        with patch(
            "src.memory.faq_cache_redis.redis.Redis.from_url",
            return_value=MagicMock(),
        ):
            cache = FAQCacheRedis("redis://localhost:6379", None, dim=None)
        with pytest.raises(ValueError, match="维度"):
            cache.ensure_index()

    def test_create_index_called(self):
        cache, client = _make_cache(dim=1024)
        cache.ensure_index()
        assert client.ft.return_value.create_index.called

    def test_index_exists_is_idempotent(self):
        cache, client = _make_cache(dim=1024)
        client.ft.return_value.create_index.side_effect = redis.ResponseError(
            "Index already exists"
        )
        cache.ensure_index()  # 不应抛异常


class TestCheck:
    class FakeDoc:
        # FT.SEARCH 返回的 doc.id 是完整 key(含 faq: 前缀)
        id = "faq:abc123"
        question = "工伤保险怎么认定"
        answer = "根据工伤保险条例第十四条..."
        sources = '[{"law_name": "工伤保险条例"}]'
        related_laws = '["law1"]'
        confidence = "1.0"
        hit_count = "1"

        def __init__(self, score="0.05"):
            self.score = score

    def _hit_cache(self):
        cache, client = _make_cache(dim=1024)
        cache.ensure_index()
        cache._embedder.embed_query.return_value = [0.1] * 1024
        client.ft.return_value.search.return_value = MagicMock(
            docs=[self.FakeDoc(score="0.05")]  # 距离 0.05 → 相似度 0.95
        )
        return cache, client

    def test_hit_returns_answer(self):
        cache, _ = self._hit_cache()
        result = cache.check("工伤保险怎么认定")
        assert result is not None
        assert result["answer"].startswith("根据工伤保险条例")
        assert result["score"] == 0.95

    def test_hit_renews_ttl_and_counts(self):
        cache, client = self._hit_cache()
        cache.check("问题")
        # 命中续期 + hit_count 递增
        assert client.expire.called
        assert client.hincrby.called

    def test_hit_renews_law_index_ttl(self):
        """回归：FAQ 命中续期时，其失效索引 Set 同步续期（避免索引提前过期）"""
        cache, client = self._hit_cache()  # FakeDoc.related_laws = '["law1"]'
        cache.check("问题")
        expire_keys = [c[0][0] for c in client.expire.call_args_list]
        # 既续期了 FAQ key，也续期了 faq:law:law1
        assert any(k == "faq:abc123" for k in expire_keys)
        assert any(k == "faq:law:law1" for k in expire_keys)

    def test_hit_refreshes_ttl_on_correct_key(self):
        """回归：doc.id 是完整 key 时,不得再拼前缀(否则产生 faq:faq:... 且 TTL 不刷新)"""
        cache, client = self._hit_cache()
        cache.check("问题")
        # EXPIRE 必须作用于 faq:abc123,而不是 faq:faq:abc123
        expire_keys = [c[0][0] for c in client.expire.call_args_list]
        assert "faq:abc123" in expire_keys
        assert not any(k.startswith("faq:faq:") for k in expire_keys)
        # HINCRBY 同样作用于正确 key
        hincr_key = client.hincrby.call_args[0][0]
        assert hincr_key == "faq:abc123"

    def test_short_doc_id_still_works(self):
        """防御分支:doc.id 为短 id 时自动补前缀"""
        cache, client = _make_cache(dim=1024)
        cache._embedder.embed_query.return_value = [0.1] * 1024
        doc = self.FakeDoc(score="0.05")
        doc.id = "abc123"
        client.ft.return_value.search.return_value = MagicMock(docs=[doc])
        cache.check("问题")
        expire_keys = [c[0][0] for c in client.expire.call_args_list]
        assert "faq:abc123" in expire_keys

    def test_below_threshold_miss(self):
        cache, client = _make_cache(dim=1024)
        cache._embedder.embed_query.return_value = [0.1] * 1024
        client.ft.return_value.search.return_value = MagicMock(
            docs=[self.FakeDoc(score="0.5")]  # 距离 0.5 → 相似度 0.5 < 0.95
        )
        assert cache.check("不相关问题") is None

    def test_no_docs_miss(self):
        cache, client = _make_cache(dim=1024)
        cache._embedder.embed_query.return_value = [0.1] * 1024
        client.ft.return_value.search.return_value = MagicMock(docs=[])
        assert cache.check("问题") is None

    def test_index_missing_returns_none(self):
        cache, client = _make_cache(dim=1024)
        cache._embedder.embed_query.return_value = [0.1] * 1024
        client.ft.return_value.search.side_effect = redis.ResponseError("no such index")
        assert cache.check("问题") is None


class TestStore:
    def test_low_confidence_skipped(self):
        cache, client = _make_cache(dim=1024)
        cache.store("问题", "答案", confidence=0.5)
        assert not client.hset.called

    def test_store_writes_and_indexes(self):
        cache, client = _make_cache(dim=1024)
        cache._embedder.embed_query.return_value = [0.2] * 1024
        cache.store(
            "问题", "答案",
            sources=[{"law_name": "民法典"}],
            related_laws=["law1", "law2"],
            confidence=0.9,
        )
        assert client.hset.called
        assert client.expire.called
        # 每个关联法律写入一个 Set 索引
        assert client.sadd.call_count == 2
        laws = [c[0][0] for c in client.sadd.call_args_list]
        assert "faq:law:law1" in laws and "faq:law:law2" in laws

    def test_store_index_set_shares_ttl(self):
        """回归：失效索引 Set 与 FAQ 同 TTL，避免 FAQ 过期后留下永不过期的僵尸引用"""
        cache, client = _make_cache(dim=1024)
        cache._embedder.embed_query.return_value = [0.2] * 1024
        cache.store(
            "问题", "答案", related_laws=["law1"], confidence=0.9,
        )
        # 对 faq:law:law1 也设置了 TTL（与 faq key 相同）
        expire_keys = [c[0][0] for c in client.expire.call_args_list]
        assert any(k == "faq:law:law1" for k in expire_keys)


class TestInvalidateByLaw:
    def test_deletes_related_entries(self):
        cache, client = _make_cache(dim=1024)
        client.smembers.return_value = {"id1", "id2"}
        count = cache.invalidate_by_law("law1")
        assert count == 2
        assert client.pipeline.called

    def test_no_entries(self):
        cache, client = _make_cache(dim=1024)
        client.smembers.return_value = set()
        assert cache.invalidate_by_law("law1") == 0
        assert not client.pipeline.called


class TestCleanExpired:
    def test_noop_returns_zero(self):
        cache, _ = _make_cache(dim=1024)
        assert cache.clean_expired() == 0
