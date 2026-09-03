"""
B 类场景人工确认标记存储（M3 / F12 v1，决策 D-M3-9a）。

F12 v1 采用「进入图之前的一次确认」（路径 A，spike 结论）：确认发生在任何
LLM 调用之前，图未开始执行、无状态需保存恢复，因此**不需要 checkpointer /
interrupt**，一个「本会话已确认」标记即可。2026-09-03（D-0903-7）起前端确认后
**在同一 SSE 连接上直接续跑生成**（`/api/chat/confirm` approved=True 写标记并返回
事件流）；标记语义保持兼容——旧客户端/其他入口随后重发 /api/chat/stream
（同一 session_id）时，服务端查到标记同样正常执行。

设计要点（对照 docs/M3-F12-人工确认技术方案.md §3.3 / §4 / §5）：
- 存储：Redis `SETEX`（原子写入 + TTL），key = ``lexagent:confirm:{user}:{session}``；
  value = 已确认的 query 原文——恢复时比对，用户换问题（风险 R7）则要求重新确认；
- TTL：默认 600 秒（Q7 决策：超时 10 分钟，超时取消、提示重新发起；
  不自动按默认参数继续执行——B 类多为文书/合同/报告，擅自代答有法律风险）；
- 降级（D-M3-8 同款原则，统计/辅助组件故障不拖垮主链路）：
  - Redis 不可用 → 退化进程内存储（单进程部署，Dockerfile 现状，可用）；
  - 读取异常 → fail-open（视为已确认，B 类回落 A 类直接执行）；
- 线程安全：进程内回退用锁保护（与 CostBudget 同款）。

测试：单测用 ``ConfirmationStore(redis_url="")``（进程内模式），
不打真实 Redis / 网络。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_KEY_PREFIX = "lexagent:confirm"
# 标记里存 query 原文用于换题比对（R7），超长截断即可
_QUERY_MAX_CHARS = 500


def _scope(user_id: str, session_id: str) -> str:
    """标记键的会话作用域（chat 接口免认证，匿名用户统一 anon）。"""
    user = (user_id or "").strip() or "anon"
    session = (session_id or "").strip() or "anon"
    return f"{user}:{session}"


class ConfirmationStore:
    """人工确认标记的存取（Redis 优先 / 进程内回退 / 读异常 fail-open）。

    Args:
        redis_url: Redis 连接串；为空或连接失败时使用进程内存储
        ttl_seconds: 标记有效期（秒），Q7 决策默认 600（10 分钟）
    """

    def __init__(self, redis_url: str = "", ttl_seconds: int = 600):
        self._ttl = max(1, int(ttl_seconds or 600))
        self._client = None
        self._lock = threading.Lock()
        # 进程内回退：{scope: (query, expires_at)}，expires_at 用单调时钟
        self._memory: dict[str, tuple[str, float]] = {}

        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url, decode_responses=True)
                client.ping()
                self._client = client
                logger.info("人工确认标记启用 Redis 存储")
            except Exception as e:
                self._client = None
                logger.warning(f"Redis 不可用，人工确认标记退化进程内存储: {e}")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def confirm(self, user_id: str, session_id: str, query: str) -> bool:
        """写入确认标记。返回 True 表示写入成功（含进程内回退）。"""
        scope = _scope(user_id, session_id)
        q = (query or "").strip()[:_QUERY_MAX_CHARS]
        if self._client is not None:
            try:
                self._client.setex(f"{_KEY_PREFIX}:{scope}", self._ttl, q)
                return True
            except Exception as e:
                logger.warning(f"写入确认标记失败，退化进程内存储: {e}")
        with self._lock:
            self._memory[scope] = (q, time.monotonic() + self._ttl)
        return True

    def is_confirmed(self, user_id: str, session_id: str, query: str) -> bool:
        """是否已确认（且 query 与确认时一致）。

        任何存储异常一律 fail-open（视为已确认，B 类回落 A 类直接执行），
        确认机制故障不阻断主链路——宁可少一次确认，不可答不出。
        """
        scope = _scope(user_id, session_id)
        q = (query or "").strip()[:_QUERY_MAX_CHARS]
        try:
            if self._client is not None:
                val = self._client.get(f"{_KEY_PREFIX}:{scope}")
            else:
                with self._lock:
                    entry = self._memory.get(scope)
                    if entry is None:
                        return False
                    stored, expires_at = entry
                    if time.monotonic() >= expires_at:
                        self._memory.pop(scope, None)
                        return False
                    val = stored
        except Exception as e:
            logger.warning(f"读取确认标记失败（fail-open 回落 A 类）: {e}")
            return True
        # 换题（R7）：确认后的 query 与当前不一致 → 要求重新确认
        return bool(val) and val == q

    def clear(self, user_id: str, session_id: str) -> None:
        """清除标记（用户取消确认时调用）。"""
        scope = _scope(user_id, session_id)
        with self._lock:
            self._memory.pop(scope, None)
        if self._client is not None:
            try:
                self._client.delete(f"{_KEY_PREFIX}:{scope}")
            except Exception as e:
                logger.warning(f"清除确认标记失败: {e}")


# ---------------------------------------------------------------------------
# 全局单例（graph 默认与 /chat/confirm 接口共用同一实例）
# ---------------------------------------------------------------------------

_store: "ConfirmationStore | None" = None
_store_lock = threading.Lock()


def get_confirmation_store() -> ConfirmationStore:
    """获取全局 ConfirmationStore 单例（按 src.config 构建单例）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from src.config import CONFIRMATION_TTL_SECONDS, REDIS_URL

                _store = ConfirmationStore(redis_url=REDIS_URL, ttl_seconds=CONFIRMATION_TTL_SECONDS)
    return _store


def reset_confirmation_store() -> None:
    """重置全局单例（测试 monkeypatch 配置后调用）。"""
    global _store
    with _store_lock:
        _store = None
