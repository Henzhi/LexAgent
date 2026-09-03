"""
可观测性 — 检索质量日志。

每次查询记录完整的性能指标和检索链路信息到 query_logs 表。
用于：性能瓶颈分析、检索质量追踪、高频问题发现、成本核算。

v0.7（2026-09-03 连接池整改）：
  - 不再持有常驻连接：每次落库从进程级连接池借用（`src.db.pool.db_connection`），
    写完自动归还——并发写入不再被「单连接 + 锁」串行化，也无需断线重连逻辑
    （连接失效归还后由池丢弃，下次借新连接）
  - 写失败仍是 debug 级日志吞掉——观测组件故障绝不拖垮主链路
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class QueryLogger:
    """检索质量日志记录器（连接池借用，无状态连接管理）

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
        # conn_string 保留仅为兼容旧构造签名；实际连接统一走池（池不可用退化直连）
        self._conn_string = conn_string

    # ------------------------------------------------------------------
    # 兼容 API（旧版持有常驻连接；现无连接可关，close 保留为 no-op）
    # ------------------------------------------------------------------

    def close(self):
        return None

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

        # 每次落库从连接池借用一条连接（写完自动归还）；并发写入互不阻塞，
        # 连接失效由池处理。任何异常 debug 吞掉——观测故障不拖垮主链路。
        try:
            from src.db.pool import db_connection

            with db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO query_logs "
                        "(request_id, user_id, query, intent, retrieved_count, reranked_count, "
                        " faq_cache_hit, memory_docs_used, llm_tokens_used, total_latency_ms, stage_timings) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            self._request_id,
                            self._user_id,
                            self._query,
                            self._intent or "",
                            self._retrieved_count,
                            self._reranked_count,
                            self._faq_cache_hit,
                            self._memory_docs_used,
                            self._llm_tokens,
                            total_latency,
                            json.dumps(self._stages, ensure_ascii=False) if self._stages else "{}",
                        ),
                    )
                conn.commit()
        except Exception as e:
            logger.debug(f"QueryLogger 写入失败: {e}")
