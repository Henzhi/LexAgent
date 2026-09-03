"""
F15 /api/usage/* 路由单元测试。

鉴权：路由本身挂 `require_registered_user`（硬鉴权，由 test_route_auth_guard
守护）；这里用 FastAPI dependency_overrides 替换为假用户，专注功能正确性。

数据：不依赖真实 PG——monkeypatch `psycopg2.connect` 命中 db_connection()
直连路径，usage_store 各读函数由 mock cursor 提供行。
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def authed_client() -> TestClient:
    from src.api import routes as routes_mod
    from src.api.main import app

    def _fake_user() -> str:
        return "tester"

    app.dependency_overrides[routes_mod.require_registered_user] = _fake_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _patch_connect(monkeypatch):
    """默认打桩 psycopg2.connect 返回空 cursor（usage_store 读空→补零/空列表）。"""
    from unittest.mock import MagicMock

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchall.return_value = []
    cur.fetchone.return_value = (0,)
    conn.cursor.return_value = cur

    def _connect(*a, **k):
        return conn

    monkeypatch.setattr("psycopg2.connect", _connect)


class TestUsageSummary:
    def test_summary_ok(self, authed_client):
        r = authed_client.get("/api/usage/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["days"] == 7
        assert len(data["items"]) == 7
        assert data["items"][-1]["day"] == date.today().isoformat()

    def test_summary_days_param(self, authed_client):
        r = authed_client.get("/api/usage/summary?days=14")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 14

    def test_summary_bad_days_422(self, authed_client):
        r = authed_client.get("/api/usage/summary?days=0")
        assert r.status_code == 422
        r2 = authed_client.get("/api/usage/summary?days=999")
        assert r2.status_code == 422


class TestUsageDetail:
    def test_detail_ok(self, authed_client):
        r = authed_client.get("/api/usage/detail")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_detail_with_day(self, authed_client):
        r = authed_client.get(f"/api/usage/detail?day={date.today().isoformat()}")
        assert r.status_code == 200

    def test_detail_bad_limit_422(self, authed_client):
        r = authed_client.get("/api/usage/detail?limit=5000")
        assert r.status_code == 422


class TestUsageBreakdown:
    def test_breakdown_group_source(self, authed_client):
        r = authed_client.get("/api/usage/breakdown?group=source")
        assert r.status_code == 200
        assert r.json()["group"] == "source"

    def test_breakdown_group_model(self, authed_client):
        r = authed_client.get("/api/usage/breakdown?group=model")
        assert r.status_code == 200

    def test_breakdown_invalid_group_422(self, authed_client):
        r = authed_client.get("/api/usage/breakdown?group=evil")
        assert r.status_code == 422


class TestUsagePricing:
    def test_get_pricing_defaults(self, authed_client):
        r = authed_client.get("/api/usage/pricing")
        assert r.status_code == 200
        items = r.json()["items"]
        keys = {it["key"] for it in items}
        assert "llm.deepseek.input_hit_cny_per_m" in keys
        assert "pkulaw.point_cny" in keys

    def test_put_pricing_validates_body(self, authed_client):
        r = authed_client.put("/api/usage/pricing", json={"items": "not-a-list"})
        assert r.status_code == 422

    def test_put_pricing_ok(self, authed_client):
        r = authed_client.put(
            "/api/usage/pricing",
            json={"items": [{"key": "llm.deepseek.input_miss_cny_per_m", "value": 2.0}]},
        )
        assert r.status_code == 200
        assert r.json()["updated"] == 1

    def test_put_pricing_unknown_key_ignored(self, authed_client):
        r = authed_client.put(
            "/api/usage/pricing",
            json={"items": [{"key": "hack.key", "value": 999.0}]},
        )
        assert r.status_code == 200
        assert r.json()["updated"] == 0
