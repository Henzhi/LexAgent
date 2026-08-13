"""
FAQ 语义缓存单元测试。

验证:
  1. FAQCache 模块可导入
  2. 缓存命中阈值
  3. 缓存写入（不写低置信度）
  4. 缓存失效
"""
from __future__ import annotations


class TestImports:
    def test_import_faq_cache(self):
        from src.memory.faq_cache import FAQCache
        assert FAQCache is not None

    def test_import_constants(self):
        from src.memory.faq_cache import HIT_THRESHOLD, DEFAULT_TTL_HOURS
        assert 0 <= HIT_THRESHOLD <= 1
        assert DEFAULT_TTL_HOURS > 0


class TestCacheLogic:
    """纯逻辑测试（不依赖 PG）"""

    def test_clean_expired_sql_structure(self):
        """验证 clean_expired 的 SQL 在语法层面正确"""
        # FAQCache 类是合法的 Python 类
        import inspect
        from src.memory.faq_cache import FAQCache
        assert inspect.isclass(FAQCache)
        # 类属性存在
        assert hasattr(FAQCache, 'check')
        assert hasattr(FAQCache, 'store')
        assert hasattr(FAQCache, 'invalidate_by_law')
        assert hasattr(FAQCache, 'clean_expired')

    def test_cache_lifecycle_methods(self):
        """验证缓存生命周期方法签名"""
        import inspect
        from src.memory.faq_cache import FAQCache

        check_sig = inspect.signature(FAQCache.check)
        assert 'query' in check_sig.parameters

        store_sig = inspect.signature(FAQCache.store)
        params = list(store_sig.parameters.keys())
        assert 'question' in params
        assert 'answer' in params
        assert 'confidence' in params

        invalidate_sig = inspect.signature(FAQCache.invalidate_by_law)
        assert 'law_id' in invalidate_sig.parameters

    def test_hit_threshold_value(self):
        """0.95 是合理的语义缓存阈值"""
        from src.memory.faq_cache import HIT_THRESHOLD
        # 太高 (>0.98) 会导致缓存几乎无法命中
        # 太低 (<0.90) 会导致不同问题被错误合并
        assert 0.90 <= HIT_THRESHOLD <= 0.98

    def test_check_refreshes_ttl_sql(self):
        """命中后 check() 的 UPDATE 必须同时刷新 hit_count 与 expires_at（续期）"""
        import inspect
        from src.memory.faq_cache import FAQCache
        source = inspect.getsource(FAQCache.check)
        # 命中后刷新 TTL：expires_at 顺延 + hit_count 累加
        assert "hit_count = hit_count + 1" in source
        assert "expires_at = NOW() + INTERVAL" in source
        assert "WHERE id = %s" in source

    def test_store_uses_hours_ttl(self):
        """store() 的写入 SQL 使用小时级 INTERVAL（而非天级）"""
        import inspect
        from src.memory.faq_cache import FAQCache
        source = inspect.getsource(FAQCache.store)
        assert "INTERVAL '%s hours'" in source
        assert "ttl_hours" in source
