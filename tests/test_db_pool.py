"""PG 连接池守护测试（2026-09-03 审查整改，中期项「引入连接池」）。

背景：全项目 14 处裸 psycopg2.connect，其中 auth.py 每次 Token 校验都新建
连接——并发下 PG 连接数无上限、建连开销白吃延迟。引入进程级
ThreadedConnectionPool 统一出口后，这里守住几条关键契约：

1. 归还语义：db_connection() 正常/异常退出都必须归还连接（借而不还 = 池泄漏）；
2. fail-open：池初始化失败 / getconn 抛错时退化为一次性直连，不拖垮主链路；
3. 并发安全：多线程并发借用，同时在借数量不超过 maxconn；
4. 惰性创建：导入本模块（连带 import 链上的整个应用）绝不能触发真实建连。

所有测试不依赖真实 PG（fake pool + fake connection）。
"""

from __future__ import annotations

import threading

import pytest

from src.db import pool as pool_mod
from src.db.pool import db_connection, get_pool, pool_init_error, reset_pool


class _FakeRawPool:
    """模拟 psycopg2 ThreadedConnectionPool 的借还行为（含超发保护）。"""

    def __init__(self, minconn: int, maxconn: int):
        self.minconn = minconn
        self.maxconn = maxconn
        self._idle: list = []
        self._out: list = []
        self._lock = threading.Lock()
        self.created = 0
        self.closed_all = False

    def getconn(self):
        with self._lock:
            if self._idle:
                conn = self._idle.pop()
            elif len(self._out) < self.maxconn:
                conn = _FakeConnection(f"conn-{self.created}")
                self.created += 1
            else:
                raise RuntimeError("connection pool exhausted")
            self._out.append(conn)
            return conn

    def putconn(self, conn, close: bool = False):
        with self._lock:
            if conn in self._out:
                self._out.remove(conn)
            if close:
                return
            self._idle.append(conn)

    def closeall(self):
        with self._lock:
            self.closed_all = True
            self._idle.clear()
            self._out.clear()


class _FakeConnection:
    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self.rolled_back = 0
        self.committed = 0

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args, **kwargs):
        pass

    def fetchone(self):
        return None


@pytest.fixture
def fake_pool():
    """注入假池并保证测试后清理（防止污染其它测试的单例状态）。

    VectorAwarePool 已是 ThreadedConnectionPool 子类（无组合 _pool 属性），
    这里在 __new__ 出的实例上直接绑定借还方法。
    """
    p = _FakeRawPool(minconn=2, maxconn=3)
    holder = pool_mod.VectorAwarePool.__new__(pool_mod.VectorAwarePool)
    holder.getconn = p.getconn
    holder.putconn = p.putconn
    holder.closeall = p.closeall
    pool_mod._pool = holder
    pool_mod._pool_init_error = ""
    yield p
    reset_pool()


class TestBorrowReturnSemantics:
    def test_connection_returned_on_success(self, fake_pool):
        with db_connection() as conn:
            conn.commit()
        assert len(fake_pool._idle) == 1, "正常退出必须归还连接"
        assert len(fake_pool._out) == 0

    def test_connection_returned_on_exception(self, fake_pool):
        with pytest.raises(ValueError), db_connection():
            raise ValueError("boom")
        assert len(fake_pool._idle) == 1, "异常退出也必须归还连接，否则池泄漏"
        assert len(fake_pool._out) == 0

    def test_rollback_called_on_exception(self, fake_pool):
        with pytest.raises(ValueError), db_connection() as conn:
            raise ValueError("boom")
        assert conn.rolled_back == 1, "异常退出必须回滚未提交事务，防止污染下一个借用方"

    def test_repeated_borrows_reuse_pool(self, fake_pool):
        for _ in range(5):
            with db_connection():
                pass
        # maxconn=3，5 次借用应全部来自池的复用（无一次真实新建超过池容量）
        assert fake_pool.created <= fake_pool.maxconn

    def test_concurrent_borrows_bounded_by_maxconn(self, fake_pool):
        """并发借用时同时借出数不得超过 maxconn，且全部能归还。"""
        errors: list[str] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait()
                with db_connection() as conn:
                    assert len(fake_pool._out) <= fake_pool.maxconn
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"并发借用出错: {errors}"
        assert len(fake_pool._out) == 0, "并发结束后所有连接都应归还"


class TestFailOpen:
    def test_no_pool_falls_back_to_direct_connect(self, monkeypatch):
        """池为 None（初始化失败）时退化为一次性直连。"""
        reset_pool()
        monkeypatch.setattr(pool_mod, "get_pool", lambda: None)

        created = []

        def _fake_connect(dsn):  # noqa: ARG001
            conn = _FakeConnection("direct")
            created.append(conn)
            return conn

        import psycopg2

        monkeypatch.setattr(psycopg2, "connect", _fake_connect)
        with db_connection() as conn:
            conn.commit()
        assert created and created[0].closed, "直连模式用完必须 close"

    def test_pool_init_failure_does_not_raise(self, monkeypatch):
        """池初始化失败不抛异常——主链路不能因为连接池挂掉。"""
        reset_pool()

        def _boom(*args, **kwargs):
            raise RuntimeError("pg down")

        monkeypatch.setattr(pool_mod, "VectorAwarePool", _boom)
        assert get_pool() is None
        assert "pg down" in pool_init_error()

    def test_getconn_failure_falls_back_to_direct(self, monkeypatch):
        """getconn 抛错（池耗尽等）→ 退化为一次性直连，不影响本次请求。"""
        holder = pool_mod.VectorAwarePool.__new__(pool_mod.VectorAwarePool)
        holder.getconn = staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("pool exhausted")))
        holder.putconn = staticmethod(lambda *a, **k: None)
        pool_mod._pool = holder
        pool_mod._pool_init_error = ""

        created = []

        def _fake_connect(dsn):  # noqa: ARG001
            conn = _FakeConnection("direct2")
            created.append(conn)
            return conn

        import psycopg2

        monkeypatch.setattr(psycopg2, "connect", _fake_connect)
        with db_connection() as conn:
            conn.commit()
        assert created and created[0].closed


class TestLazyCreation:
    def test_import_does_not_create_pool(self, monkeypatch):
        """导入/获取单例在 PG 不可达时绝不能抛异常（惰性 + fail-open）。"""
        reset_pool()

        def _boom(*args, **kwargs):
            raise RuntimeError("no pg")

        monkeypatch.setattr(pool_mod, "VectorAwarePool", _boom)
        assert get_pool() is None
        # 二次调用：失败被记住，不再反复尝试建池
        assert get_pool() is None

    def test_reset_pool_clears_state(self, fake_pool, monkeypatch):
        """reset 后单例清空；重建走打桩（不依赖本机是否真有 PG 在跑）。"""
        created = []

        class _StubPool:
            def __init__(self, *args, **kwargs):
                created.append(1)

            def closeall(self):
                pass

        monkeypatch.setattr(pool_mod, "VectorAwarePool", _StubPool)
        assert get_pool() is not None
        reset_pool()
        assert pool_init_error() == ""
        assert get_pool() is not None, "reset 后应允许重新惰性建池"
        assert len(created) == 1
        assert fake_pool.closed_all, "reset 时应关闭池内所有连接"


class TestAuthUsesPool:
    """auth 热路径必须走池（防回退：曾几何时每次 Token 校验都新建连接）。"""

    def test_verify_token_goes_through_db_connection(self, monkeypatch):
        """verify_token 的落库路径必须经 db_connection（而非裸 psycopg2.connect）。"""
        import contextlib

        from src.api import auth as auth_mod

        borrowed = []

        @contextlib.contextmanager
        def _fake_db_connection():
            conn = _FakeConnection("auth-conn")
            borrowed.append(conn)
            yield conn

        monkeypatch.setattr(auth_mod, "db_connection", _fake_db_connection)
        monkeypatch.setattr(auth_mod, "_token_cache", {})

        # 未命中缓存 → 落库查询 → 未命中返回 None
        assert auth_mod.verify_token("x" * 32) is None
        assert len(borrowed) == 1, "verify_token 必须经 db_connection 借用连接"

    def test_auth_module_no_longer_has_bare_get_db(self):
        """守护：不允许在 auth 里复活 _get_db() 这类『借出即失联』的裸连接函数。"""
        from src.api import auth as auth_mod

        assert not hasattr(auth_mod, "_get_db"), (
            "_get_db() 曾每次调用新建连接且与池生命周期脱钩（借出的池化连接被 close = 池泄漏），"
            "禁止复活；请使用 src.db.pool.db_connection()"
        )
