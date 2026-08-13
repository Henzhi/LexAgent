"""API 路由单元测试 — TestClient（pg_required 标记需要 PostgreSQL）"""
from __future__ import annotations

import pytest

_pg_cache = None

def _pg_available():
    global _pg_cache
    if _pg_cache is None:
        try:
            import psycopg2
            from src.config import PG_CONN
            conn = psycopg2.connect(PG_CONN, connect_timeout=1)
            conn.close()
            _pg_cache = True
        except Exception:
            _pg_cache = False
    return _pg_cache

pg_required = pytest.mark.skipif(not _pg_available(), reason="PostgreSQL 不可用——启动 PG 后测试")


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "llm_model" in data
        assert "doc_count" in data


class TestAuth:
    def _reg_and_login(self, client, username="tester", password="test123456"):
        client.post("/api/auth/register", json={
            "username": username, "password": password,
        })
        r2 = client.post("/api/auth/login", json={
            "username": username, "password": password,
        })
        assert r2.status_code == 200
        return r2.json()["token"]

    @pg_required
    def test_register_and_login(self, client):
        token = self._reg_and_login(client, "ut1", "pass123456")
        assert len(token) > 20
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["anonymous"] is False
        assert len(data["user_id"]) > 0

    @pg_required
    def test_wrong_password(self, client):
        r = client.post("/api/auth/login", json={"username": "ut1", "password": "wrong"})
        assert r.status_code == 401

    @pg_required
    def test_list_conversations(self, client):
        token = self._reg_and_login(client, "ut2", "pass123456")
        r = client.get("/api/conversations", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert isinstance(r.json(), list)
