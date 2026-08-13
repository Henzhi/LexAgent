"""
pgvector 存储层单元测试。

验证:
  1. PgvectorStore 模块可导入
  2. PgvectorStoreRetriever 可导入
  3. 纯 PG 架构（FAISS 已移除）
"""
from __future__ import annotations


class TestImports:
    def test_import_pgvector_store(self):
        from src.knowledge.pgvector_store import PgvectorStore
        assert PgvectorStore is not None

    def test_import_retriever(self):
        from src.rag.retriever import PgvectorStoreRetriever
        assert PgvectorStoreRetriever is not None

    def test_no_faiss_left(self):
        """v0.6 纯 PG：FAISSRetriever / vector_store 模块应已移除"""
        import pytest
        with pytest.raises(ImportError):
            from src.rag.retriever import FAISSRetriever  # noqa: F401
        with pytest.raises(ImportError):
            import src.embedding.vector_store  # noqa: F401


class TestRetriever:
    def test_row_to_doc(self):
        from src.rag.retriever import PgvectorStoreRetriever
        row = {
            "content": "第一条 为了惩罚犯罪...",
            "score": 0.9521,
            "law_name": "中华人民共和国刑法",
            "chapter": "第一编 总则",
            "section": "",
            "article_range": "第一条",
            "chunk_type": "article",
        }
        doc = PgvectorStoreRetriever._row_to_doc(row)
        assert doc.content == "第一条 为了惩罚犯罪..."
        assert doc.score == 0.9521
        assert doc.law_name == "中华人民共和国刑法"
        assert doc.chapter == "第一编 总则"
        assert doc.article_range == "第一条"
        assert doc.citation == "中华人民共和国刑法 · 第一条"


class TestChunkPagination:
    """get_document_chunks 分页：验证 LIMIT/OFFSET 拼接与参数传递（不连真实 PG）"""

    def _make_store_with_mock(self, fetched):
        from unittest.mock import MagicMock
        from src.knowledge.pgvector_store import PgvectorStore

        store = PgvectorStore.__new__(PgvectorStore)
        store._conn = MagicMock()
        store._ensure_connection = MagicMock()

        cur = MagicMock()
        cur.fetchall.return_value = fetched
        store._conn.cursor.return_value.__enter__.return_value = cur
        return store, cur

    def test_paginated_query_includes_limit_offset(self):
        store, cur = self._make_store_with_mock([("id1", "article", "第一条...", "bge-m3", None, "2026-01-01")])
        rows = store.get_document_chunks("doc-1", limit=50, offset=100)
        # SQL 必须包含 LIMIT/OFFSET，且参数按 (doc_id, limit, offset) 顺序传入
        sql, params = cur.execute.call_args[0]
        assert "LIMIT" in sql and "OFFSET" in sql
        assert params == ("doc-1", 50, 100)
        assert len(rows) == 1

    def test_non_paginated_has_no_limit(self):
        store, cur = self._make_store_with_mock([])
        rows = store.get_document_chunks("doc-1")
        sql, params = cur.execute.call_args[0]
        assert "LIMIT" not in sql and "OFFSET" not in sql
        assert params == ("doc-1",)
        assert rows == []

    def test_count_document_chunks(self):
        store, cur = self._make_store_with_mock([])
        cur.fetchone.return_value = (1323,)
        assert store.count_document_chunks("doc-1") == 1323
