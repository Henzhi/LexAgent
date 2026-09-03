"""
请求级用量上下文（F15 修正 2026-09-03）：把 request_id / session_id / user_id
随「正在执行的请求线程」传播到埋点落库处。

背景：usage_logs 的 request_id 此前全空——LLM/Tavily/pkulaw 埋点各自 record 时
只知道自己的一次调用，不知道所属的请求（ChatModel 是长驻单例、callback 构造时
还没有 request_id）。结果用量面板无法按「一次提问」聚合，只能整桶统计。

方案：threading.local 上下文。流式请求在 SSE 桥接的后台线程里串行执行整个
生成器（LLM/工具调用都在该线程），同步 /api/chat 在 FastAPI 线程池线程执行——
两种形态都是「一线程一请求」，入口 set、出口 clear 即可，无需改 callback 签名。

埋点侧兜底：usage_store.record_usage 落库时若 request_id/session_id 为空、
user_id 是占位默认值，则用当前线程的 ctx 填充（没有 ctx 时保持原值，不影响
无请求上下文的调用点，如独立脚本/测试）。
"""

from __future__ import annotations

import threading
from typing import Any

_local = threading.local()


def set_usage_ctx(*, request_id: str = "", session_id: str = "", user_id: str = "") -> None:
    """进入一次请求时设置上下文（调用方负责在 finally 中 clear）。"""
    _local.ctx = {
        "request_id": request_id or "",
        "session_id": session_id or "",
        "user_id": user_id or "",
    }


def clear_usage_ctx() -> None:
    """请求结束（含异常路径）时清除，防线程复用串味。"""
    if hasattr(_local, "ctx"):
        del _local.ctx


def usage_ctx() -> dict[str, str] | None:
    """读取当前线程的请求上下文；未设置返回 None。"""
    return getattr(_local, "ctx", None)


def resolve_ctx(
    *, request_id: str | None = None, session_id: str | None = None, user_id: str = ""
) -> tuple[str | None, str | None, str]:
    """把调用方显式传入的 id 与线程 ctx 合并（ctx 为兜底，显式值优先）。"""
    ctx: dict[str, Any] | None = usage_ctx()
    if not ctx:
        return request_id, session_id, user_id
    rid = request_id or ctx.get("request_id") or None
    sid = session_id or ctx.get("session_id") or None
    uid = user_id
    if not uid or uid == "default":
        uid = ctx.get("user_id") or user_id
    return rid, sid, uid
