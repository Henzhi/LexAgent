"""路由鉴权守护测试（2026-09-01 代码审查整改）。

问题：`require_registered_user`（硬鉴权）与 `get_current_user`（软鉴权，匿名
回退 anonymous user）只挂在部分路由上，`/api/rewrite`、`/api/budget`、
`/api/knowledge/documents*`、`/api/chat/stream/resume`、`/api/crawl/status`
等接口匿名可直接调用——rewrite 绕过 F14 预算熔断（每次真实消耗一次 LLM），
knowledge 列表匿名可拉全库正文，resume 匿名可重放他人会话流。

本文件是**守护型**测试：遍历路由表断言涉敏路由必须挂硬鉴权。以后新增路由
漏加 Depends(require_registered_user) 时，这里直接转红，而不是等到渗透测试。
"""

from fastapi.routing import APIRoute

from src.api.routes import auth_router, router

# --- 白名单：确实公开、必须无鉴权的接口 ------------------------------------
# /api/chat* 与 /api/conversations* 走软鉴权（get_current_user，匿名回退
# anonymous 命名空间），是产品设计决策，不在本守护范围。
PUBLIC_ROUTES = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/register"),
    ("GET", "/api/health"),
    # 2026-09-03 HttpOnly Cookie 迁移新增：登出必须**无鉴权**——凭据可能已失效
    # （401 后清理残留 Cookie），要求登出先登录是逻辑死结
    ("POST", "/api/auth/logout"),
}

# --- 必须硬鉴权（require_registered_user）的接口 ----------------------------
# 每一项都对应一次真实的安全整改，删依赖前先想清楚为什么。
MUST_BE_HARD = {
    ("POST", "/api/rewrite"): "每次调用真实消耗一次 LLM，匿名可刷 = 绕过 F14 预算熔断",
    ("GET", "/api/budget"): "暴露内部 API 用量与成本，运维数据不应匿名可读",
    ("GET", "/api/knowledge/status/{task_id}"): "匿名可枚举 task_id 探测他人上传",
    ("GET", "/api/knowledge/documents"): "匿名可分页遍历整个知识库",
    ("GET", "/api/knowledge/documents/{doc_id}/chunks"): "匿名可拉知识库正文全文",
    ("GET", "/api/chat/stream/resume"): "匿名可枚举 request_id 重放他人会话流",
    ("GET", "/api/crawl/status/{task_id}"): "与 POST /api/crawl 同一管理链路，口径须一致",
    ("GET", "/api/crawl/types"): "与 POST /api/crawl 同一管理链路，口径须一致",
}


def _route_map() -> dict[tuple[str, str], APIRoute]:
    """{(method, path): route}，路径已含 /api 前缀（auth_router 挂在 /api/auth）。"""
    out = {}
    for prefix, r_ in (("/api", router), ("/api/auth", auth_router)):
        for r in r_.routes:
            if isinstance(r, APIRoute):
                out[(next(iter(r.methods)), f"{prefix}{r.path}")] = r
    return out


def _has_hard_auth(route: APIRoute) -> bool:
    """检查路由依赖树里是否直接挂了 require_registered_user。"""
    from src.api.auth import require_registered_user

    stack = [route.dependant]
    while stack:
        d = stack.pop()
        if d.call is require_registered_user:
            return True
        stack.extend(d.dependencies)
    return False


def test_must_be_hard_authed_routes():
    routes = _route_map()
    for key, why in MUST_BE_HARD.items():
        assert key in routes, f"路由表里找不到 {key}，守护清单与实现脱节，请同步更新 MUST_BE_HARD"
        assert _has_hard_auth(routes[key]), f"{key[0]} {key[1]} 缺硬鉴权：{why}"


def test_public_routes_stay_public():
    """白名单接口必须保持无鉴权（防止有人顺手把登录接口也锁死）。"""
    routes = _route_map()
    for key in PUBLIC_ROUTES:
        assert key in routes, f"路由表里找不到 {key}，守护清单与实现脱节"
        assert not _has_hard_auth(routes[key]), f"{key[0]} {key[1]} 不应加硬鉴权"


def test_no_unknown_unauthed_route_slips_in():
    """除白名单外，任何路由要么硬鉴权、要么在软鉴权已知集合里。

    新增路由如果两者都不是，必须显式加进 PUBLIC_ROUTES 或本测试的豁免清单，
    让"公开"成为一个明示决定而不是默认疏漏。
    """
    soft_known = {
        ("GET", "/api/auth/me"),
        ("GET", "/api/conversations"),
        ("DELETE", "/api/conversations/{session_id}"),
        ("GET", "/api/conversations/{session_id}"),
        ("POST", "/api/conversations/{session_id}"),
        ("POST", "/api/chat"),
        ("POST", "/api/chat/cancel"),
        ("POST", "/api/chat/confirm"),
        ("POST", "/api/chat/stream"),
    }
    routes = _route_map()
    unaccounted = []
    for key, route in routes.items():
        if key in PUBLIC_ROUTES or key in MUST_BE_HARD or key in soft_known:
            continue
        if not _has_hard_auth(route):
            unaccounted.append(key)
    assert not unaccounted, (
        f"以下路由无硬鉴权且未登记（软鉴权 {sorted(soft_known)} 是产品决策）：{sorted(unaccounted)}。"
        "请补 Depends(require_registered_user)，或显式加入 PUBLIC_ROUTES / soft_known。"
    )
