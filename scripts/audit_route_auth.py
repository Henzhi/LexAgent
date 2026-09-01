"""
路由鉴权审计脚本（代码审查整改配套工具）。

用途：
    静态扫描 FastAPI 应用的所有路由，列出「路径 + 方法 + 挂载的 Depends 依赖」，
    把没有鉴权依赖的路由单独标出来，供人工核对是否需要补鉴权。

为什么做成脚本而不是一次性走查：
    审查报告发现「知识库读接口无鉴权」这类问题属于**新增路由时最容易被复制粘贴
    漏掉**的类型——写死的 checklist 会随路由增加过期，而脚本每次跑都是当前真相。
    CI 里挂上它可以防止新增无鉴权路由。

跑法:
    uv run python scripts/audit_route_auth.py                 # 审计报告
    uv run python scripts/audit_route_auth.py --strict        # 有未鉴权路由则退出码 1
    uv run python scripts/audit_route_auth.py --allow a,b,c   # 豁免白名单（公开路由）

退出码:
    0 一切正常 / 1 存在未鉴权且未豁免的路由（仅 --strict）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证可从任意 cwd 导入 src 包
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 视为「已鉴权」的依赖名（硬鉴权：匿名返回 401）
AUTH_DEPS_HARD = {"require_registered_user"}
# 软鉴权：匿名回退，不拒绝请求——单独标注，提示人工确认是否符合预期
AUTH_DEPS_SOFT = {"get_current_user"}

# 默认公开路由白名单（产品上就是免认证，见 AGENTS.md「API」段）
DEFAULT_ALLOWLIST = {
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/health"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/chat/cancel"),
    ("POST", "/api/chat/confirm"),
    ("GET", "/api/rewrite/suggest"),  # 若不存在则忽略（白名单只做减法）
}


def _dep_names(route) -> list[str]:
    """从路由的 dependant 树里收集所有依赖的可调用名（含嵌套 Depends）。"""
    names: list[str] = []

    def _walk(dependant) -> None:
        for call in getattr(dependant, "dependencies", []) or []:
            fn = getattr(call, "call", None)
            if fn is not None:
                names.append(getattr(fn, "__name__", repr(fn)))
            sub = getattr(call, "call", None)
            # 嵌套依赖：Depends 里的函数自己也可能 Depends 了别人
            inner = getattr(sub, "__wrapped__", None)
            if inner is not None:
                names.append(getattr(inner, "__name__", repr(inner)))
        for sub_dep in getattr(dependant, "dependencies", []) or []:
            _walk(sub_dep)

    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        _walk(dependant)
        # 顶层直接挂的 Depends（函数签名里的参数默认值）
        for param in getattr(dependant, "query_params", []) or []:
            pass
    return names


def _signature_deps(route) -> list[str]:
    """读路由函数的签名默认值，找出 `Depends(...)` 里那个可调用的名字。"""
    import inspect

    fn = getattr(route, "endpoint", None)
    if fn is None:
        return []
    out: list[str] = []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return out
    for p in sig.parameters.values():
        default = p.default
        call = getattr(default, "dependency", None)
        if call is None:
            # FastAPI 的 Depends 对象 attribute 是 .dependency
            call = getattr(default, "call", None)
        if call is not None:
            out.append(getattr(call, "__name__", repr(call)))
    return out


def collect_routes():
    from src.api.main import app

    rows = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or not path.startswith("/api"):
            continue
        fn = getattr(route, "endpoint", None)
        deps = set(_dep_names(route)) | set(_signature_deps(route))
        hard = sorted(deps & AUTH_DEPS_HARD)
        soft = sorted(deps & AUTH_DEPS_SOFT)
        rows.append(
            {
                "methods": sorted(m for m in methods if m not in ("HEAD", "OPTIONS")),
                "path": path,
                "name": getattr(fn, "__name__", ""),
                "hard": hard,
                "soft": soft,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="审计 FastAPI 路由的鉴权依赖")
    ap.add_argument("--strict", action="store_true", help="存在未鉴权路由时以退出码 1 结束（供 CI 使用）")
    ap.add_argument("--allow", default="", help="额外豁免的 'METHOD:/path' 列表，逗号分隔")
    args = ap.parse_args()

    extra_allow = set()
    for item in filter(None, (s.strip() for s in args.allow.split(","))):
        if ":" in item:
            m, p = item.split(":", 1)
            extra_allow.add((m.upper(), p))
        else:
            extra_allow.add(("*", item))

    allowlist = DEFAULT_ALLOWLIST | extra_allow

    rows = collect_routes()
    rows.sort(key=lambda r: (r["path"], ",".join(r["methods"])))

    unprotected: list[dict] = []
    print(f"{'METHOD':<8} {'PATH':<48} {'AUTH':<10} ENDPOINT")
    print("-" * 110)
    for r in rows:
        for m in r["methods"]:
            if r["hard"]:
                auth, mark = "hard", "✅"
            elif r["soft"]:
                auth, mark = "soft", "⚠️ "
            else:
                auth, mark = "NONE", "❌"
            print(f"{m:<8} {r['path']:<48} {auth:<10} {mark} {r['name']}")
            if not r["hard"] and not r["soft"]:
                key = (m, r["path"])
                if key in allowlist or ("*", r["path"]) in allowlist:
                    continue
                unprotected.append({"method": m, "path": r["path"], "name": r["name"], "auth": auth})

    print()
    print(f"路由总数: {len(rows)}　硬鉴权: {sum(1 for r in rows if r['hard'])}　"
          f"软鉴权: {sum(1 for r in rows if not r['hard'] and r['soft'])}　无鉴权: {sum(1 for r in rows if not r['hard'] and not r['soft'])}")

    if unprotected:
        print(f"\n❌ 以下 {len(unprotected)} 个路由既无硬鉴权也无软鉴权（且不在白名单）：")
        for u in unprotected:
            print(f"   {u['method']:<7} {u['path']:<46} {u['name']}")
        print("\n处理建议：")
        print("  · 确实是公开接口 → 加进本脚本 DEFAULT_ALLOWLIST，并同步更新 AGENTS.md")
        print("  · 需要登录 → 路由签名加 `_user: str = Depends(require_registered_user)`")
        if args.strict:
            return 1
    else:
        print("\n✅ 所有路由均有鉴权依赖，或已在白名单中显式豁免。")

    # 软鉴权提示：这些接口匿名可访问（回退 anonymous），需人工确认是否符合预期
    soft_only = [r for r in rows if r["soft"] and not r["hard"]]
    if soft_only:
        print(f"\n⚠️  以下 {len(soft_only)} 个路由只有软鉴权（匿名会回退 anonymous user，不拒绝）：")
        for r in soft_only:
            print(f"   {','.join(r['methods']):<7} {r['path']}")
        print("  → 若这些接口处理用户私有数据，请改成 require_registered_user")

    return 0


if __name__ == "__main__":
    sys.exit(main())
