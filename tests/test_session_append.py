"""会话增量保存（mode=append）守护测试（2026-09-03 审查整改）。

背景：前端此前每轮结束上传整个 messages 数组，对话越长 payload 越大
（O(n²) 流量）。整改后前端只传本轮新增消息，服务端在数据库内用 JSONB
`||` 原子拼接。

守住三条契约：
1. store.append_session 必须用 SQL 级拼接（不读回全量再写回——那既慢又有并发窗口）；
2. 路由层 mode 参数：append 走增量、replace 走全量、非法值 400；
3. 记忆固化触发判断用「追加后的总条数」，且固化拿到的是全量历史。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_store(monkeypatch):
    """不触碰真实 PG 的 ConversationStore。

    v0.7 连接池整改后：方法内 `db_connection()`（模块级导入）被替换为
    产出 fake_conn 的上下文管理器；`_schema_ready` 预置 True 跳过建表 DDL，
    让断言聚焦在目标 SQL 上。
    """
    import contextlib

    from src.api.conversation_store import ConversationStore

    store = ConversationStore.__new__(ConversationStore)
    store._conn_string = "postgresql://fake"
    store._schema_ready = True
    store._schema_lock = __import__("threading").Lock()

    fake_conn = MagicMock()

    @contextlib.contextmanager
    def _fake_db_connection():
        yield fake_conn

    monkeypatch.setattr("src.api.conversation_store.db_connection", _fake_db_connection)
    return store, fake_conn


def _cur(conn, fetchone_result=None):
    """给 fake_conn 挂上 with cursor() 返回的 mock 游标，并记录 execute。"""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    if fetchone_result is not None:
        cur.fetchone.return_value = fetchone_result
    conn.cursor.return_value.__enter__.return_value = cur
    return cur


class TestAppendSessionStore:
    def test_append_uses_sql_jsonb_concat(self, fake_store):
        """追加必须在 SQL 内完成（|| 拼接），不是读回全量再覆盖。"""
        store, conn = fake_store
        cur = _cur(conn, fetchone_result=(6,))

        total = store.append_session(user_id="u1", session_id="s1", messages=[{"role": "user", "content": "hi"}])

        assert total == 6
        sql = cur.execute.call_args[0][0]
        assert "conversations.messages ||" in sql, "必须用 JSONB || 在数据库内拼接"
        assert "RETURNING jsonb_array_length" in sql, "应返回追加后的总条数"

    def test_append_serializes_chinese(self, fake_store):
        store, conn = fake_store
        cur = _cur(conn, fetchone_result=(2,))

        store.append_session(user_id="u", session_id="s", messages=[{"role": "user", "content": "行政拘留多久"}])
        args = cur.execute.call_args[0][1]
        assert "行政拘留多久" in args[2], "中文必须按 UTF-8 原样序列化（ensure_ascii=False）"

    def test_append_schema_ddl_runs_once_then_skipped(self, monkeypatch):
        """建表 DDL 惰性执行一次（_schema_ready 置位后不再跑）。"""
        import contextlib

        from src.api.conversation_store import ConversationStore

        store = ConversationStore.__new__(ConversationStore)
        store._conn_string = "postgresql://fake"
        store._schema_ready = False
        store._schema_lock = __import__("threading").Lock()

        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__.return_value = cur
        conn.cursor.return_value.__enter__.return_value = cur

        @contextlib.contextmanager
        def _fake_db_connection():
            yield conn

        monkeypatch.setattr("src.api.conversation_store.db_connection", _fake_db_connection)

        store.save_session("u", "s", [{"role": "user", "content": "q"}])
        assert store._schema_ready is True
        all_sql = " ".join(c[0][0] for c in cur.execute.call_args_list)
        assert "CREATE TABLE IF NOT EXISTS conversations" in all_sql

        cur.reset_mock()
        store.save_session("u", "s", [{"role": "user", "content": "q2"}])
        # 第二次操作不再跑 DDL（execute 只应被 INSERT 调用）
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert not any("CREATE TABLE" in s for s in sqls)


class TestSaveSessionRoute:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from src.api.main import app

        return TestClient(app)

    def _post(self, client, body, token="t" * 32):
        return client.post("/api/conversations/s1", json=body, headers={"Authorization": f"Bearer {token}"})

    def test_append_mode_calls_store_append(self, client, monkeypatch):
        saved = {}
        store = MagicMock()
        store.append_session.return_value = 5

        def _get_store():
            return store

        monkeypatch.setattr("src.api.conversation_store.get_conversation_store", _get_store)
        monkeypatch.setattr("src.api.auth._token_cache", {("t" * 32)[:64]: "u1"})

        # verify_token 走内存缓存需要 hash 对得上，直接 patch 掉
        monkeypatch.setattr("src.api.auth.verify_token", lambda token: "u1")
        monkeypatch.setattr("src.memory.conversation.SUMMARY_TRIGGER_ROUNDS", 99)  # 关闭记忆固化分支

        resp = self._post(client, {"messages": [{"role": "user", "content": "q"}], "mode": "append"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "total": 5}
        assert store.append_session.call_count == 1
        saved["mode"] = "append"

    def test_replace_mode_default_keeps_old_behavior(self, client, monkeypatch):
        store = MagicMock()
        monkeypatch.setattr("src.api.conversation_store.get_conversation_store", lambda: store)
        monkeypatch.setattr("src.api.auth.verify_token", lambda token: "u1")
        monkeypatch.setattr("src.memory.conversation.SUMMARY_TRIGGER_ROUNDS", 99)

        resp = self._post(client, {"messages": [{"role": "user", "content": "q"}]})
        assert resp.status_code == 200
        store.save_session.assert_called_once()
        store.append_session.assert_not_called()

    def test_invalid_mode_returns_400(self, client, monkeypatch):
        store = MagicMock()
        monkeypatch.setattr("src.api.conversation_store.get_conversation_store", lambda: store)
        monkeypatch.setattr("src.api.auth.verify_token", lambda token: "u1")

        resp = self._post(client, {"messages": [], "mode": "upsert"})
        assert resp.status_code == 400
        store.save_session.assert_not_called()
        store.append_session.assert_not_called()

    def test_memory_persist_uses_full_history_in_append_mode(self, client, monkeypatch):
        """记忆固化需要完整会话：append 模式下必须从库里读回全量，不能用增量片段。"""
        store = MagicMock()
        store.append_session.return_value = 6
        full = [{"role": "user", "content": f"m{i}"} for i in range(6)]
        store.load_history.return_value = full
        monkeypatch.setattr("src.api.conversation_store.get_conversation_store", lambda: store)
        monkeypatch.setattr("src.api.auth.verify_token", lambda token: "u1")
        monkeypatch.setattr("src.memory.conversation.SUMMARY_TRIGGER_ROUNDS", 4)

        captured = {}
        monkeypatch.setattr(
            "src.api.routes._persist_memory_background",
            lambda user_id, session_id, messages: captured.update(messages=messages),
        )

        resp = self._post(client, {"messages": [{"role": "user", "content": "new"}], "mode": "append"})
        assert resp.status_code == 200
        assert captured.get("messages") == full, "记忆固化必须拿到全量历史，而非本轮增量"
