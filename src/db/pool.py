"""数据库连接池（2026-09-03 审查整改，中期项「引入连接池」）。

背景：全项目 14 处裸 `psycopg2.connect`，其中 `auth.py` 每次 Token 校验都
新建连接（每个认证请求一次，建连 ~10-30ms 且无上限）——并发下 PG 连接数
会打爆 `max_connections`，且建连开销白吃延迟。

方案：进程级 `ThreadedConnectionPool` 统一出口。

- **线程安全**：pool 的 getconn/putconn 自带锁，可多线程并发借还；
- **向量适配**：pgvector 的 `register_vector` 是按连接生效的，池内新建的每个
  连接都自动注册（`VectorAwarePool`）；
- **fail-open**：池初始化失败或取连接失败时退化为一次性直连——连接池故障
  不拖垮主链路（与「统计故障告警放行」同一原则）；
- **归还兜底**：`db_connection()` 在异常时回滚、归还时校验，杜绝把带未提交
  事务的连接还回池里污染下一个借用方。

配置（环境变量）：
- `PG_POOL_MINCONN`（默认 2）
- `PG_POOL_MAXCONN`（默认 20，对应 PG 默认 max_connections=100 的安全水位）

用法:

    from src.db.pool import db_connection

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.commit()          # 写操作自行 commit（语义与裸连接一致）
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()
_pool_init_error = ""


class VectorAwarePool:
    """ThreadedConnectionPool 的向量感知包装。

    ⚠️ 不直接继承 ThreadedConnectionPool 的原因：其 `_connect()` 在
    **构造期**就建 minconn 个连接，而本模块被测试与无 PG 环境导入时不能
    触发真实建连。这里把「池子惰性创建 + 每连接 register_vector」封装起来，
    失败时整体退化为直连模式。
    """

    def __init__(self, minconn: int, maxconn: int, dsn: str):
        import psycopg2
        from psycopg2.pool import ThreadedConnectionPool

        self._psycopg2 = psycopg2
        self._minconn = max(1, minconn)
        self._maxconn = max(self._minconn, maxconn)
        self._dsn = dsn
        self._pool = ThreadedConnectionPool(self._minconn, self._maxconn, dsn)
        self._lock = threading.Lock()

    def getconn(self):
        return self._pool.getconn()

    def putconn(self, conn, close: bool = False):
        self._pool.putconn(conn, close=close)

    def _register_vector(self, conn) -> None:
        try:
            from pgvector.psycopg2 import register_vector

            register_vector(conn)
        except Exception as e:  # pragma: no cover - pgvector 缺失属于环境问题
            logger.warning(f"register_vector 注册失败（该连接不支持向量检索）: {e}")

    def new_connection(self):
        """新建一条独立连接（池满/池外借用兜底用），自动注册向量适配。"""
        conn = self._psycopg2.connect(self._dsn)
        self._register_vector(conn)
        return conn

    def closeall(self) -> None:
        self._pool.closeall()

    @property
    def minconn(self) -> int:
        return self._minconn

    @property
    def maxconn(self) -> int:
        return self._maxconn


def get_pool() -> VectorAwarePool | None:
    """获取进程级连接池单例（惰性创建；失败时返回 None 并记住原因）。

    失败不抛异常：调用方（`db_connection`）据此退化为一次性直连。
    测试 monkeypatch 配置后调用 `reset_pool()` 重建。
    """
    global _pool, _pool_init_error
    if _pool is None and not _pool_init_error:
        with _pool_lock:
            if _pool is None and not _pool_init_error:
                try:
                    from src.config import PG_CONN

                    minconn = max(1, int(os.getenv("PG_POOL_MINCONN", "2")))
                    maxconn = max(1, int(os.getenv("PG_POOL_MAXCONN", "20")))
                    _pool = VectorAwarePool(minconn, maxconn, PG_CONN)
                    logger.info(f"PG 连接池就绪 (minconn={minconn}, maxconn={maxconn})")
                except Exception as e:
                    _pool_init_error = f"{type(e).__name__}: {e}"
                    logger.warning(f"PG 连接池初始化失败，退化为直连模式: {_pool_init_error}")
    return _pool


def reset_pool() -> None:
    """关闭并重置连接池单例（测试 / 应用 shutdown 用）。"""
    global _pool, _pool_init_error
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            except Exception as e:
                logger.warning(f"关闭连接池失败（忽略）: {e}")
        _pool = None
        _pool_init_error = ""


def pool_init_error() -> str:
    """池初始化失败原因（供 /api/health 观测；空串 = 池正常）。"""
    return _pool_init_error


@contextmanager
def db_connection():
    """借出一个连接，用完自动归还。

    与裸连接语义对齐的地方：
    - **commit/rollback 仍由调用方显式调用**——本上下文管理器不代做提交；
    - 异常退出时回滚未提交事务，防止把脏事务还回池里；
    - 池不可用（初始化失败 / getconn 抛错）时退化为一次性直连并 close。
    """
    pool = get_pool()
    conn = None
    pooled = False
    if pool is not None:
        try:
            conn = pool.getconn()
            pooled = True
        except Exception as e:
            logger.warning(f"从连接池取连接失败，退化为一次性直连: {e}")
            conn = None
    if conn is None:
        import psycopg2

        from src.config import PG_CONN

        conn = psycopg2.connect(PG_CONN)

    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    else:
        # 兜底回滚：调用方忘记 commit 时丢弃脏事务（psycopg2 归还未 idle
        # 连接时也会自行 rollback，这里显式化，语义与「close 丢弃」一致）
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if pooled:
            try:
                pool.putconn(conn)
            except Exception as e:
                logger.warning(f"连接归还失败（直接关闭以防泄漏）: {e}")
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            try:
                conn.close()
            except Exception:
                pass
