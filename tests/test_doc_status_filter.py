"""D-0907-1 守护测试：检索路径必须只召回 active 文档的 chunk。

背景：知识库一度全是 `status='active'`，旧法版本与重复入库副本一起被检索
（刑法两版 630/631 chunks 同为 active，问民企腐败会召回不含修正案十二的旧版）。
治理方式是给失效/重复文档打 status 标记，因此**每条检索路径都必须带 status 过滤**——
漏一条，标记就形同虚设（这正是本守护测试要防的回归）。

测试策略：DB-free。用假连接捕获真实执行的 SQL 字符串，断言含 status 谓词。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


class _FakeCursor:
    def __init__(self, sink: list[str]):
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink.append(" ".join(str(sql).split()))

    def fetchall(self):
        return []

    def fetchmany(self, size=1):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, sink: list[str]):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def sql_sink(monkeypatch) -> list[str]:
    """把所有 SQL 收集到列表里，供断言使用。"""
    sink: list[str] = []
    fake = lambda: _FakeConn(sink)  # noqa: E731
    # pgvector_store 在模块级 `from src.db.pool import db_connection`（名字已绑定），
    # 必须打该模块内的引用；law_centroids 是函数内延迟导入，打源头即可。
    monkeypatch.setattr("src.db.pool.db_connection", fake)
    monkeypatch.setattr("src.knowledge.pgvector_store.db_connection", fake)
    return sink


def test_pgvector_search_filters_inactive(sql_sink):
    from src.knowledge.pgvector_store import PgvectorStore

    store = PgvectorStore.__new__(PgvectorStore)
    store.search([0.1] * 1024, top_k=5, embedding_model="bge-m3")
    assert sql_sink, "检索未执行任何 SQL"
    assert any("d.status = 'active'" in sql for sql in sql_sink)


def test_fetch_all_active_chunks_filters_inactive(sql_sink):
    """BM25 索引构建源：重复副本若进索引会挤占词频与召回位次。"""
    from src.knowledge.pgvector_store import PgvectorStore

    store = PgvectorStore.__new__(PgvectorStore)
    store.fetch_all_active_chunks()
    assert any("d.status = 'active'" in sql for sql in sql_sink)


def test_law_centroids_filters_inactive(sql_sink):
    """法名质心：失效版本混入会稀释质心，让法名推断加权失真。"""
    from src.rag.law_centroids import LawCentroids

    LawCentroids._load_rows()
    assert any("d.status = 'active'" in sql for sql in sql_sink)


def test_ingest_normalizes_title_before_dedupe():
    """D-0907-1：入库标题必须归一，否则尾空格会让同一部法被重复入库。"""
    from src.knowledge.ingestion.pipeline import normalize_document_title

    assert normalize_document_title(" 中华人民共和国公司法(2023修订) ") == ("中华人民共和国公司法(2023修订)")
    assert normalize_document_title("某法  名称\n\t带空白 ") == "某法 名称 带空白"
    assert normalize_document_title(None) == ""
