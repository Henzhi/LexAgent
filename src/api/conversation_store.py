"""对话持久化（pgvector PostgreSQL）—— 每个会话一条 JSONB 记录"""
from __future__ import annotations

import json
import logging
import threading
from functools import wraps

import psycopg2
from src.config import PG_CONN

logger = logging.getLogger(__name__)


def _locked(method):
    """串行化对共享 PG 连接的访问（psycopg2 连接非线程安全）。

    会话读写由 FastAPI sync 端点在线程池执行，多请求并发必须保护共享连接。
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_lock", None)
        if lock is None:
            # 防御：兼容绕过 __init__ 的构造方式（如测试 mock）
            lock = threading.Lock()
            self._lock = lock
        with lock:
            return method(self, *args, **kwargs)
    return wrapper


class ConversationStore:
    def __init__(self, conn_string: str = PG_CONN):
        self._conn_string = conn_string
        self._conn = psycopg2.connect(conn_string)
        self._create_table()

    def _ensure_connection(self):
        """检查连接是否存活，断开则自动重连"""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception as e:
            logger.warning(f"PG 连接已断开，尝试重连... ({e})")
            try:
                self._conn.close()
            except Exception as close_e:
                logger.debug(f"关闭旧连接失败（可忽略）: {close_e}")
            self._conn = psycopg2.connect(self._conn_string)
            logger.info("PG 重连成功")

    def _create_table(self):
        """建表（兼容旧表结构自动迁移）"""
        with self._conn.cursor() as cur:
            # 创建 users 表（如果不存在）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    username VARCHAR(64) UNIQUE NOT NULL,
                    password_hash VARCHAR(256) NOT NULL DEFAULT '',
                    token_hash VARCHAR(128) NOT NULL DEFAULT '',
                    display_name VARCHAR(128),
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            try:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256) NOT NULL DEFAULT ''")
            except Exception as e:
                logger.warning(f"users 表迁移 password_hash 失败（可忽略）: {e}")
            try:
                cur.execute("ALTER TABLE users ALTER COLUMN token_hash SET DEFAULT ''")
            except Exception as e:
                logger.warning(f"users 表迁移 token_hash 失败（可忽略）: {e}")
            cur.execute("""
                INSERT INTO users (id, username, password_hash, token_hash, display_name)
                VALUES ('00000000-0000-0000-0000-000000000000', '__anonymous__', '', '', '匿名用户')
                ON CONFLICT (id) DO NOTHING
            """)
            # 创建 conversations 表（每个 session 一条记录，JSONB 存全部消息）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    user_id UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000' REFERENCES users(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL,
                    messages JSONB NOT NULL DEFAULT '[]',
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            # 兼容旧表加列
            try:
                cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000' REFERENCES users(id) ON DELETE CASCADE")
            except Exception as e:
                logger.warning(f"conversations 表迁移 user_id 失败（可忽略）: {e}")
            try:
                cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS messages JSONB NOT NULL DEFAULT '[]'")
            except Exception as e:
                logger.warning(f"conversations 表迁移 messages 失败（可忽略）: {e}")
            try:
                cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()")
            except Exception as e:
                logger.warning(f"conversations 表迁移 updated_at 失败（可忽略）: {e}")
            # 重建为唯一索引
            try:
                cur.execute("DROP INDEX IF EXISTS idx_conv_user_session")
            except Exception as e:
                logger.warning(f"conversations 唯一索引清理失败（可忽略）: {e}")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_user_session ON conversations(user_id, session_id)")
        self._conn.commit()

    @_locked
    def save_session(self, user_id: str, session_id: str, messages: list[dict]):
        """保存/更新整个会话的 JSON 消息数组"""
        self._ensure_connection()
        messages_json = json.dumps(messages, ensure_ascii=False)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversations (user_id, session_id, messages, created_at, updated_at)
                VALUES (%s, %s, %s::jsonb, now(), now())
                ON CONFLICT (user_id, session_id)
                DO UPDATE SET messages = %s::jsonb, updated_at = now()
                """,
                (user_id, session_id, messages_json, messages_json),
            )
        self._conn.commit()

    def load_history(self, user_id: str, session_id: str, limit: int = 50) -> list[dict]:
        """加载会话的完整对话历史"""
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT messages FROM conversations WHERE user_id = %s AND session_id = %s",
                (user_id, session_id),
            )
            row = cur.fetchone()
        if row and row[0]:
            messages = row[0] if isinstance(row[0], list) else json.loads(row[0])
            return messages[-limit:] if limit else messages
        return []

    def list_sessions(self, user_id: str, limit: int = 20) -> list[dict]:
        """列出当前用户的对话会话"""
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, created_at, updated_at, messages
                FROM conversations
                WHERE user_id = %s
                ORDER BY updated_at DESC LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
        result = []
        for r in rows:
            messages = r[3] if isinstance(r[3], list) else (json.loads(r[3]) if r[3] else [])
            first_msg = ""
            for m in messages:
                if m.get("role") == "user":
                    first_msg = m.get("content", "")[:50]
                    break
            result.append({
                "session_id": r[0],
                "started": r[1].isoformat(),
                "msg_count": len(messages),
                "first_msg": first_msg,
            })
        return result

    @_locked
    def delete_session(self, user_id: str, session_id: str):
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE user_id = %s AND session_id = %s", (user_id, session_id))
        self._conn.commit()

    def close(self):
        self._conn.close()


# 全局单例
_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store
