"""
可观测性 — 检索质量日志 (v0.6)。

每次查询记录完整的性能指标和检索链路信息到 query_logs 表。
用于：性能瓶颈分析、检索质量追踪、高频问题发现、成本核算。

v0.6 改进：
  - 共享连接：不再每次写入新建 PG 连接（原 v0.5 每次 connect/close）
  - 断线自动重连：连接失效时自动重建
  - 线程安全：连接加锁，支持 Agent 路径并发调用
  - finalize 幂等：可多次调用（提前 return 分支也能记录正确字段）
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class QueryLogger:
    """检索质量日志记录器（共享连接 + 断线重连 + 线程安全）

    用法:
        qlog = QueryLogger(conn_string)

        with qlog.trace("user_001", "工伤怎么认定") as trace:
            trace.stage("intent", 200)
            trace.stage("retrieve", 350)
            trace.stage("generate", 2100)
            trace.finalize(
                retrieved_count=15,
                reranked_count=5,
                faq_cache_hit=False,
                memory_docs_used=2,
                llm_tokens=1240,
            )
    """

    def __init__(self, conn_string: str):
        self._conn_string = conn_string
        self._conn = None
        self._lock = threading.Lock()
        self._connect()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _connect(self):
        import psycopg2
        self._conn = psycopg2.connect(self._conn_string)

    def _ensure_connection(self):
        if self._conn is None or self._conn.closed:
            self._connect()

    def close(self):
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 追踪入口
    # ------------------------------------------------------------------

    @contextmanager
    def trace(self, user_id: str, query: str):
        """创建一次查询追踪上下文（异常/提前 return 时自动兜底落库）

        Usage:
            with qlog.trace(user_id, query) as trace:
                ...
        """
        trace = self.start(user_id, query)
        try:
            yield trace
        finally:
            if not trace._finalized:
                trace._save()

    def start(self, user_id: str, query: str):
        """手动开始一次追踪（供生成器用 try/finally 模式组合）"""
        trace = _QueryTrace(self, user_id, query, str(uuid.uuid4()))
        trace._start_time = time.time()
        return trace


class _QueryTrace:
    """单次查询追踪器"""

    def __init__(self, qlog: QueryLogger, user_id: str, query: str, request_id: str):
        self._qlog = qlog
        self._user_id = user_id
        self._query = query
        self._request_id = request_id
        self._start_time = 0.0
        self._stages: dict[str, float] = {}
        self._intent = ""
        self._retrieved_count = 0
        self._reranked_count = 0
        self._faq_cache_hit = False
        self._memory_docs_used = 0
        self._llm_tokens = 0
        self._finalized = False

    def stage(self, name: str, duration_ms: int):
        self._stages[name] = float(duration_ms)

    def set_intent(self, intent: str):
        self._intent = intent

    def finalize(
        self,
        retrieved_count: int = 0,
        reranked_count: int = 0,
        faq_cache_hit: bool = False,
        memory_docs_used: int = 0,
        llm_tokens: int = 0,
    ):
        """落库并标记完成（幂等：重复调用只生效一次）"""
        if self._finalized:
            return
        self._retrieved_count = retrieved_count
        self._reranked_count = reranked_count
        self._faq_cache_hit = faq_cache_hit
        self._memory_docs_used = memory_docs_used
        self._llm_tokens = llm_tokens
        self._save()
        self._finalized = True

    def _save(self):
        import json
        total_latency = int((time.time() - self._start_time) * 1000)

        qlog = self._qlog
        with qlog._lock:
            try:
                qlog._ensure_connection()
                conn = qlog._conn
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO query_logs "
                        "(request_id, user_id, query, intent, retrieved_count, reranked_count, "
                        " faq_cache_hit, memory_docs_used, llm_tokens_used, total_latency_ms, stage_timings) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            self._request_id, self._user_id, self._query,
                            self._intent or "",
                            self._retrieved_count, self._reranked_count,
                            self._faq_cache_hit, self._memory_docs_used,
                            self._llm_tokens, total_latency,
                            json.dumps(self._stages, ensure_ascii=False) if self._stages else "{}",
                        ),
                    )
                conn.commit()
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.debug(f"QueryLogger 写入失败: {e}")
