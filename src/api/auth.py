"""
认证模块：账号密码注册/登录 + Bearer Token 管理。

密码安全：
- 使用 PBKDF2-SHA256（100000 次迭代）哈希密码
- 盐值拼接在哈希值中，格式: $pbkdf2-sha256$迭代次数$salt$hash
- Token 为 64 位随机字符串，服务端只存 SHA256 哈希
"""

from __future__ import annotations

import hashlib
import secrets
import logging
import threading
import time

import psycopg2
from fastapi import Depends, Request, HTTPException

from src.db.pool import db_connection

logger = logging.getLogger(__name__)

# 内存缓存: token_hash → user_id，避免每次请求都查 DB
_token_cache: dict[str, str] = {}

# 匿名用户 ID（与 init.sql 中保持一致）
ANONYMOUS_USER_ID = "00000000-0000-0000-0000-000000000000"

# PBKDF2 参数
PBKDF2_ITERATIONS = 100_000
PBKDF2_ALGORITHM = "sha256"

# 登录防爆破：同一用户名滑动窗口内连续失败 N 次后锁定（内存实现，重启即清零）
_LOGIN_MAX_FAILURES = 5
_LOGIN_FAIL_WINDOW = 300  # 5 分钟滑动窗口（秒）
_login_failures: dict[str, list[float]] = {}
_login_failures_lock = threading.Lock()


def _check_login_allowed(username: str) -> None:
    """登录前检查：窗口内失败次数超限则拒绝（429）"""
    now = time.time()
    with _login_failures_lock:
        fails = [t for t in _login_failures.get(username, []) if now - t < _LOGIN_FAIL_WINDOW]
        _login_failures[username] = fails
        if len(fails) >= _LOGIN_MAX_FAILURES:
            retry_after = int(_LOGIN_FAIL_WINDOW - (now - fails[0])) + 1
            logger.warning(f"登录锁定中: {username}，剩余 {retry_after}s")
            raise HTTPException(status_code=429, detail=f"失败次数过多，请 {retry_after} 秒后重试")


def _record_login_failure(username: str) -> None:
    with _login_failures_lock:
        _login_failures.setdefault(username, []).append(time.time())


def _clear_login_failures(username: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(username, None)


def _hash_password(password: str) -> str:
    """
    使用 PBKDF2-SHA256 哈希密码。
    返回格式: $pbkdf2-sha256$100000$<salt_hex>$<hash_hex>
    """
    salt = secrets.token_hex(16)  # 128-bit 随机盐
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"$pbkdf2-{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def _verify_password(password: str, password_hash: str) -> bool:
    """
    验证密码是否匹配存储的哈希值。
    哈希格式: $pbkdf2-sha256$100000$<salt_hex>$<hash_hex>
    """
    if not password_hash.startswith("$pbkdf2-"):
        return False
    try:
        _, algo_config, iterations_str, salt, stored_hash = password_hash.split("$", 4)
        algorithm = algo_config.split("-", 1)[1] if "-" in algo_config else "sha256"
        iterations = int(iterations_str)
        dk = hashlib.pbkdf2_hmac(algorithm, password.encode(), salt.encode(), iterations)
        return dk.hex() == stored_hash
    except Exception:
        return False


def _hash_token(token: str) -> str:
    """对 Token 做 SHA256 哈希"""
    return hashlib.sha256(token.encode()).hexdigest()


def register_user(username: str, password: str) -> dict:
    """
    注册新用户。
    返回 {user_id, token, username}，Token 只在此时返回明文。
    """
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")

    password_hash = _hash_password(password)
    token = secrets.token_hex(32)
    token_hash = _hash_token(token)

    with db_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, password_hash, token_hash, display_name) VALUES (%s, %s, %s, %s) RETURNING id",
                    (username, password_hash, token_hash, username),
                )
                user_id = str(cur.fetchone()[0])
            conn.commit()

            _token_cache[token_hash] = user_id
            logger.info(f"新用户注册: {username} (id={user_id})")

            return {"user_id": user_id, "token": token, "username": username}
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            raise HTTPException(status_code=409, detail=f"用户名 '{username}' 已存在")


def login_user(username: str, password: str) -> dict:
    """
    登录：验证用户名密码，返回 Token。
    每次登录生成新 Token（旧 Token 失效）。
    连续失败次数超限将被临时锁定（防爆破）。
    """
    _check_login_allowed(username)
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            if row is None:
                _record_login_failure(username)
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            user_id, stored_hash = str(row[0]), row[1]
            if not _verify_password(password, stored_hash):
                _record_login_failure(username)
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            _clear_login_failures(username)

            # 生成新 Token 并更新
            # 先清掉旧 token 缓存
            cur.execute("SELECT token_hash FROM users WHERE id = %s", (user_id,))
            old_row = cur.fetchone()
            if old_row and old_row[0]:
                _token_cache.pop(old_row[0], None)

            token = secrets.token_hex(32)
            token_hash = _hash_token(token)
            cur.execute(
                "UPDATE users SET token_hash = %s WHERE id = %s",
                (token_hash, user_id),
            )
        conn.commit()

        _token_cache[token_hash] = user_id
        logger.info(f"用户登录: {username} (id={user_id})")

        return {"user_id": user_id, "token": token, "username": username}


def load_token_cache():
    """启动时将数据库中所有用户的 token 加载到内存缓存"""
    with db_connection() as conn:
        with conn.cursor() as cur:
            # 确保表存在（首次启动时可能还未创建）
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
            cur.execute("""
                INSERT INTO users (id, username, password_hash, token_hash, display_name)
                VALUES ('00000000-0000-0000-0000-000000000000', '__anonymous__', '', '', '匿名用户')
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("SELECT id, token_hash FROM users WHERE token_hash != ''")
            rows = cur.fetchall()
        conn.commit()
        for user_id, token_hash in rows:
            _token_cache[token_hash] = str(user_id)
        logger.info(f"Token 缓存加载完成: {len(_token_cache)} 个用户")


def verify_token(token: str) -> str | None:
    """验证 Token，返回 user_id；无效则返回 None"""
    if not token or len(token) < 16:
        return None
    token_hash = _hash_token(token)
    user_id = _token_cache.get(token_hash)
    if user_id:
        return user_id

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE token_hash = %s", (token_hash,))
            row = cur.fetchone()
        if row:
            user_id = str(row[0])
            _token_cache[token_hash] = user_id
            return user_id

    return None


# ---------------------------------------------------------------------------
# HttpOnly Cookie 鉴权（2026-09-03 审查整改，长期项「Token 迁移 HttpOnly Cookie」）
# ---------------------------------------------------------------------------
# 背景：Token 此前明文存于 localStorage，任何 XSS 可直接窃取（前端
# api/index.js 与 stores/auth.js 各读一次）。迁移后 Token 只活在 HttpOnly
# Cookie 里，JS 不可读——XSS 拿不到凭据；Bearer 头仍保留为兼容路径
# （CLI / curl / 第三方集成），get_current_user 先查 Cookie 再查 Header。
#
# SameSite=Strict：同站才带 Cookie，跨站 POST（CSRF）天然不带凭据；
# Secure 由 COOKIE_SECURE 控制（默认 False：部署环境可能无 HTTPS 反代，
# 有 HTTPS 时必须置 1，否则 Cookie 会明文过网）。
AUTH_COOKIE_NAME = "lexagent_token"

# Cookie 有效期：Token 本身无过期机制（登出/换新才失效），Cookie 给个宽松的
# 会话长度即可——真正的边界由服务端 Token 是否仍有效决定。
AUTH_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 天


def _cookie_secure() -> bool:
    import os

    return os.getenv("COOKIE_SECURE", "0") in ("1", "true", "True")


def set_auth_cookie(response, token: str) -> None:
    """登录/注册成功后下发 HttpOnly Cookie（Bearer 与 Cookie 双通道）。"""
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,  # JS 不可读：XSS 无法窃取凭据（本次整改核心）
        samesite="strict",  # 跨站请求不带 Cookie → CSRF 基本失效
        secure=_cookie_secure(),
        path="/",
    )


def clear_auth_cookie(response) -> None:
    """登出：清除 HttpOnly Cookie（JS 删不掉 HttpOnly，必须由服务端删）。"""
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/", secure=_cookie_secure())


def _extract_token(request: Request) -> str:
    """从请求提取 Token：优先 Cookie，其次 Authorization: Bearer。

    两条路径都进 verify_token（缓存 + 落库），不存在一方可信一方不可信的问题。
    """
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return ""


def get_current_user(request: Request) -> str:
    """
    从请求中提取用户身份，作为 FastAPI 依赖注入。

    优先级:
    1. HttpOnly Cookie（lexagent_token，前端默认通道）
    2. Authorization: Bearer <token> header（兼容 CLI/第三方）
    3. 回退到 anonymous 用户
    """
    token = _extract_token(request)
    if token:
        user_id = verify_token(token)
        if user_id:
            return user_id

    return ANONYMOUS_USER_ID


def require_registered_user(user_id: str = Depends(get_current_user)) -> str:
    """
    严格认证依赖：拒绝匿名回退，用于知识库管理/爬虫等管理接口。

    与 get_current_user 的区别：无有效 Token 时返回 401，而非匿名用户。
    """
    if user_id == ANONYMOUS_USER_ID:
        raise HTTPException(status_code=401, detail="该操作需要登录")
    return user_id
