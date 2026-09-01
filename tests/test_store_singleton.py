"""_get_store 单例守护测试（2026-09-01 代码审查整改）。

问题：`_get_store()` 原来每次调用 new 一个 `PgvectorStore`（每请求
`psycopg2.connect` 新建 PG 连接）且从不关闭——并发下 PG 连接数随请求
线性增长直至 max_connections 耗尽。

本文件用 fake 类（不连真实 PG）验证：
1. 多次调用返回同一实例（构造函数只跑一次，ensure_tables 只跑一次）；
2. close_store() 释放后下次调用重建新实例；
3. close_store() 在从未初始化时安全（幂等）；
4. close() 抛异常不向外传播（退出路径清理不制造新故障）。
"""

import threading

import pytest

from src.api import routes


class _FakeStore:
    """记录构造次数与 ensure_tables/close 调用的替身。"""

    instances: list["_FakeStore"] = []

    def __init__(self, conn_string: str):
        self.conn_string = conn_string
        self.ensure_tables_calls = 0
        self.close_calls = 0
        self.close_error: Exception | None = None
        type(self).instances.append(self)

    def ensure_tables(self):
        self.ensure_tables_calls += 1

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后都清掉单例，避免串扰其他测试。"""
    routes._STORE_SINGLETON = None
    yield
    routes._STORE_SINGLETON = None


@pytest.fixture
def fake_store_cls(monkeypatch):
    monkeypatch.setattr("src.knowledge.pgvector_store.PgvectorStore", _FakeStore)
    _FakeStore.instances = []
    return _FakeStore


def test_get_store_returns_same_instance(fake_store_cls):
    s1 = routes._get_store()
    s2 = routes._get_store()
    s3 = routes._get_store()
    assert s1 is s2 is s3
    # 构造与表检查只发生一次（原来每请求都 new + ensure_tables）
    assert len(_FakeStore.instances) == 1
    assert _FakeStore.instances[0].ensure_tables_calls == 1


def test_get_store_passes_pg_conn(fake_store_cls, monkeypatch):
    monkeypatch.setattr("src.config.PG_CONN", "postgresql://fake")
    routes._get_store()
    assert _FakeStore.instances[0].conn_string == "postgresql://fake"


def test_close_store_releases_and_resets(fake_store_cls):
    s1 = routes._get_store()
    routes.close_store()
    assert s1.close_calls == 1

    # 下次调用重建新实例（模拟测试/重启后的干净状态）
    s2 = routes._get_store()
    assert s2 is not s1
    assert len(_FakeStore.instances) == 2


def test_close_store_idempotent_when_never_created(fake_store_cls):
    routes.close_store()  # 不应抛异常
    assert _FakeStore.instances == []


def test_close_store_swallows_close_error(fake_store_cls):
    routes._get_store()
    _FakeStore.instances[0].close_error = RuntimeError("conn already broken")
    routes.close_store()  # 异常被吞，不向外传播
    assert routes._STORE_SINGLETON is None


def test_get_store_thread_safety(fake_store_cls):
    """并发首调只允许构造一个实例（双检锁的正确性）。"""
    barrier = threading.Barrier(8)
    results: list = []

    def _worker():
        barrier.wait()
        results.append(routes._get_store())

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(r) for r in results}) == 1
    assert len(_FakeStore.instances) == 1
