"""
QueryLogger（检索质量日志）单元测试。

验证 v0.7（连接池整改）关键行为：
  1. 每次落库从池借用连接（不再常驻「单连接+锁」，并发写入不被串行化）
  2. trace() 上下文管理器自动兜底落库（未 finalize 也保存）
  3. finalize 幂等
  4. start() 手动模式（供生成器路径使用）
  5. 写失败 debug 吞掉——观测组件故障绝不拖垮主链路
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fake_conn():
    """构造支持 with cursor() 的 mock PG 连接"""
    conn = MagicMock()
    conn.closed = False
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    conn.cursor.return_value = cursor
    return conn


def _make_qlog(mock_connect):
    from src.observability.query_log import QueryLogger

    fake_conn = _fake_conn()
    mock_connect.return_value = fake_conn
    qlog = QueryLogger("postgresql://fake")
    return qlog, fake_conn


class TestPerWriteBorrow:
    def test_borrows_connection_per_write(self):
        """连接池整改后：每次落库都从池借连接（测试期池关闭→每次直连）。"""
        with patch("psycopg2.connect") as mock_connect:
            qlog, _ = _fake_qlog_with(mock_connect)

            for i in range(3):
                t = qlog.start("u1", f"问题{i}")
                t.finalize(retrieved_count=1)

            # 3 次写入 → 3 次独立借连接（v0.7 语义；v0.6 是共享连接只连一次）
            assert mock_connect.call_count == 3
            assert qlog._conn_string  # 兼容字段仍存在
            assert hasattr(qlog, "close")  # close 保留为 no-op 兼容


class TestTraceContext:
    def test_auto_save_on_exit_without_finalize(self):
        """with trace() 内未 finalize,退出时自动兜底落库"""
        with patch("psycopg2.connect") as mock_connect:
            qlog, fake_conn = _fake_qlog_with(mock_connect)

            with qlog.trace("u1", "工伤怎么认定") as trace:
                trace.stage("intent", 10)

            # 退出 with → 自动 _save
            executed = fake_conn.cursor.return_value.execute
            assert executed.called

    def test_trace_records_metadata(self):
        with patch("psycopg2.connect") as mock_connect:
            qlog, fake_conn = _fake_qlog_with(mock_connect)

            with qlog.trace("u1", "行政拘留最长多久") as trace:
                trace.stage("intent", 100)
                trace.stage("retrieve", 350)
                trace.stage("generate", 2100)
                trace.finalize(
                    retrieved_count=15,
                    reranked_count=5,
                    faq_cache_hit=True,
                    memory_docs_used=2,
                )

            sql, params = fake_conn.cursor.return_value.execute.call_args[0]
            assert "query_logs" in sql
            assert params[2] == "行政拘留最长多久"  # query
            assert params[4] == 15  # retrieved_count
            assert params[5] == 5  # reranked_count
            assert params[6] is True  # faq_cache_hit
            assert params[7] == 2  # memory_docs_used


class TestFinalizeIdempotent:
    def test_double_finalize_writes_once(self):
        with patch("psycopg2.connect") as mock_connect:
            qlog, fake_conn = _fake_qlog_with(mock_connect)
            t = qlog.start("u1", "q")
            t.finalize(retrieved_count=1)
            t.finalize(retrieved_count=99)  # 幂等:忽略

            execute = fake_conn.cursor.return_value.execute
            assert execute.call_count == 1


class TestManualStart:
    def test_start_pattern_for_generators(self):
        """生成器路径:start() + try/finally 兜底"""
        with patch("psycopg2.connect") as mock_connect:
            qlog, fake_conn = _fake_qlog_with(mock_connect)
            t = qlog.start("u1", "q")
            t.set_intent("law_lookup")
            t.finalize(retrieved_count=3)
            assert t._finalized

    def test_start_without_finalize_saves_in_finally(self):
        with patch("psycopg2.connect") as mock_connect:
            qlog, fake_conn = _fake_qlog_with(mock_connect)
            t = qlog.start("u1", "q")
            # 模拟生成器 finally 兜底
            t._save()
            assert fake_conn.cursor.return_value.execute.called


class TestWriteFailureSwallowed:
    def test_db_error_does_not_propagate(self):
        """落库失败只 debug 日志，绝不向上抛——观测组件故障不拖垮主链路。"""
        with patch("psycopg2.connect") as mock_connect:
            qlog, _ = _fake_qlog_with(mock_connect)
            # 让 execute 抛异常（模拟 PG 抖动）
            mock_connect.return_value.cursor.return_value.execute.side_effect = RuntimeError("pg down")

            t = qlog.start("u1", "q")
            t.finalize(retrieved_count=1)  # 不应抛
            assert t._finalized is True

    def test_no_connect_when_query_logger_created(self):
        """构造不再建连（连接改由池在落库时才借）——惰性 & 无泄漏。"""
        with patch("psycopg2.connect") as mock_connect:
            qlog, _ = _fake_qlog_with(mock_connect)
            assert mock_connect.call_count == 0  # 构造零连接


def _fake_qlog_with(mock_connect):
    from src.observability.query_log import QueryLogger

    fake_conn = _fake_conn()
    mock_connect.return_value = fake_conn
    qlog = QueryLogger("postgresql://fake")
    return qlog, fake_conn
