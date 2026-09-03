"""Token HttpOnly Cookie 迁移守护测试（2026-09-03 审查整改，长期项）。

背景：Token 明文存 localStorage，XSS 可直接窃取。迁移后 Token 写入
HttpOnly + SameSite=Strict Cookie，JS 不可读；Bearer 头保留为兼容通道。

DB-free：所有注册/登录/校验均打桩（verify_token / register_user / login_user），
不依赖 CI 是否有真实 PG——本文件守护的是 Cookie 的**下发/携带/清除**契约，
不是数据库本身。

守住的契约：
1. login/register 响应带 Set-Cookie（HttpOnly + SameSite=Strict）；
2. 带 Cookie 的后续请求能通过 get_current_user / require_registered_user；
3. logout 清 Cookie；
4. Bearer 头兼容路径仍然可用（CLI/第三方调用方不受影响）；
5. 无 Cookie 无 Header → 严格路由 401（原有行为不回退）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.auth import AUTH_COOKIE_NAME


@pytest.fixture
def auth_client(monkeypatch):
    from src.api.main import app

    client = TestClient(app)

    # --- DB-free 打桩：注册/登录产罐头 Token，verify_token 认识它 ---
    token = f"t{uuid.uuid4().hex}"

    def _fake_register(*args, **kwargs):
        return {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "token": token,
            "username": kwargs.get("username", "") or args[0],
        }

    def _fake_login(*args, **kwargs):
        return {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "token": token,
            "username": kwargs.get("username", "") or args[0],
        }

    monkeypatch.setattr("src.api.routes.register_user", _fake_register)
    monkeypatch.setattr("src.api.routes.login_user", _fake_login)
    # verify_token 是 get_current_user 内部唯一落库点；打桩后整条链不碰 PG
    monkeypatch.setattr(
        "src.api.auth.verify_token", lambda t: "11111111-1111-1111-1111-111111111111" if t == token else None
    )

    client._test_token = token
    return client


def _register(client: TestClient, username: str = ""):
    """注册并返回响应（用户名随机，Cookie 由路由设置）。"""
    if not username:
        username = f"u{uuid.uuid4().hex[:10]}"
    resp = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert resp.status_code == 200, resp.text
    return resp


class TestLoginSetsHttpOnlyCookie:
    def test_register_sets_cookie(self, auth_client):
        resp = _register(auth_client)
        assert AUTH_COOKIE_NAME in resp.cookies, "注册后必须下发鉴权 Cookie"
        assert resp.cookies.get(AUTH_COOKIE_NAME), "Cookie 值不能为空"

    def test_login_sets_cookie(self, auth_client):
        resp = auth_client.post("/api/auth/login", json={"username": "any", "password": "secret123"})
        assert resp.status_code == 200
        assert AUTH_COOKIE_NAME in resp.cookies

    def test_cookie_is_httponly_and_samesite(self, auth_client):
        """Cookie 必须 HttpOnly + SameSite=strict——XSS 读不到、跨站 CSRF 不带。"""
        resp = _register(auth_client)
        set_cookie_headers = [v for v in resp.headers.get_list("set-cookie") if v.startswith(AUTH_COOKIE_NAME)]
        assert set_cookie_headers, "响应头缺少 Set-Cookie"
        assert "HttpOnly" in set_cookie_headers[0], "Cookie 缺少 HttpOnly——JS 仍可窃取凭据"
        assert "SameSite=strict" in set_cookie_headers[0], "Cookie 缺少 SameSite=strict——CSRF 防护失效"


class TestCookieAuthenticates:
    def test_cookie_works_for_me(self, auth_client):
        """带 Cookie 的请求应被识别为已登录（不再是匿名）。"""
        _register(auth_client)
        resp = auth_client.get("/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anonymous"] is False
        assert data["user_id"] != "00000000-0000-0000-0000-000000000000"

    def test_cookie_passes_strict_route(self, auth_client):
        """硬鉴权路由（/api/budget，无外部依赖）带 Cookie 能过鉴权。"""
        _register(auth_client)
        resp = auth_client.get("/api/budget")
        assert resp.status_code == 200, f"带 Cookie 访问严格路由应放行，实际 {resp.status_code}"

    def test_no_credential_strict_route_401(self, auth_client):
        """无 Cookie 无 Header → 严格路由仍 401（行为不回退）。"""
        resp = auth_client.get("/api/budget")
        assert resp.status_code == 401


class TestLogout:
    def test_logout_clears_cookie(self, auth_client):
        _register(auth_client)
        assert auth_client.get("/api/auth/me").json()["anonymous"] is False

        resp = auth_client.post("/api/auth/logout")
        assert resp.status_code == 200
        # 清除 Cookie：响应中应带 Set-Cookie 过期指令
        clearing = [v for v in resp.headers.get_list("set-cookie") if v.startswith(AUTH_COOKIE_NAME)]
        assert clearing and ("Max-Age=0" in clearing[0] or "expires" in clearing[0].lower()), "登出必须下发过期 Cookie"

        # TestClient 的 cookie jar 已移除该 Cookie → 回到匿名
        assert auth_client.get("/api/auth/me").json()["anonymous"] is True


class TestBearerFallbackKept:
    def test_header_auth_still_works(self, auth_client):
        """Bearer 兼容通道不能断：CLI / curl / 第三方集成依赖它。"""
        resp = _register(auth_client)
        token = resp.json()["token"]

        bare = TestClient(__import__("src.api.main", fromlist=["app"]).app)
        bare_get = bare.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert bare_get.status_code == 200
        assert bare_get.json()["anonymous"] is False

    def test_body_still_returns_token(self, auth_client):
        """响应体保留 token（Bearer 模式调用方从 body 取）。"""
        resp = _register(auth_client)
        assert len(resp.json().get("token", "")) >= 32
