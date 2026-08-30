"""
SSE 事件日志（M3 / D-M3-12 断线重连）：每个流式请求的事件带递增 seq 写入
Redis List；被动断线后生成任务继续跑完并持续写入，重连请求带 after_seq
先重放后跟新（`GET /api/chat/stream/resume`）。

设计要点（对照 DECISIONS D-M3-12）：
- **事件日志是重连补发的唯一真相源**：bridge 的 worker 线程先把事件写入本
  日志再投递在线队列——在线消费者丢弃事件无妨，重连用户从日志读取；
- key = ``lexagent:stream:{request_id}:events``（RPUSH + EXPIRE，
  TTL = STREAM_LOG_TTL_SECONDS 默认 600s，与确认标记同量级）；
- **终局标记** ``{"type": "__stream_end__"}``：生成自然完成 / 出错收尾 /
  被取消时由 worker finally 追加——重连方读到它即知流已终局（取消后重连
  用户重放到此为止，不会无限等待）；
- **区分主动取消与被动断线**（关键设计）：/chat/cancel → 立即停（省 Token，
  现状不变）；网络断开 → 继续跑完（LLM 成本已沉没，跑完才能补发）。本模块
  只负责存取，该区分逻辑在 routes._bridge_sync_stream；
- **降级**（D-M3-8 同款原则）：Redis 不可用退化进程内（单进程部署可用）；
  写入失败由调用方告警后继续投递在线链路，日志故障绝不阻断主链路。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "lexagent:stream"
_END_EVENT_TYPE = "__stream_end__"


class StreamEventLog:
    """按 request_id 存取 SSE 事件序列（Redis 优先 / 进程内回退）。

    Args:
        redis_url: Redis 连接串；为空或连接失败时使用进程内存储
        ttl_seconds: 日志保留时长（秒），默认 600（10 分钟，够一次断线重连）
    """

    def __init__(self, redis_url: str = "", ttl_seconds: int = 600):
        self._ttl = max(60, int(ttl_seconds or 600))
        self._client = None
        self._lock = threading.Lock()
        # 进程内回退：{stream_id: (events, expires_at)}，events 为带 seq 的 payload 列表
        self._memory: dict[str, tuple[list[dict[str, Any]], float]] = {}

        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url, decode_responses=True)
                client.ping()
                self._client = client
                logger.info("SSE 事件日志启用 Redis 存储")
            except Exception as e:
                self._client = None
                logger.warning(f"Redis 不可用，SSE 事件日志退化进程内存储: {e}")

    # ------------------------------------------------------------------
    # 写入（仅 worker 线程单写者，seq 用 LLEN/len 推进）
    # ------------------------------------------------------------------

    def append(self, stream_id: str, payload: dict[str, Any]) -> int:
        """追加事件并返回其 seq（从 1 递增）。写入失败向上抛，由调用方决定降级。"""
        if self._client is not None:
            key = f"{_KEY_PREFIX}:{stream_id}:events"
            seq = int(self._client.llen(key)) + 1
            pipe = self._client.pipeline()
            pipe.rpush(key, json.dumps({**payload, "seq": seq}, ensure_ascii=False))
            pipe.expire(key, self._ttl)
            pipe.execute()
            return seq
        with self._lock:
            events, _ = self._memory_get_or_create(stream_id)
            seq = len(events) + 1
            events.append({**payload, "seq": seq})
            return seq

    def append_end(self, stream_id: str, error: str | None = None) -> None:
        """追加终局标记（生成完成 / 出错收尾 / 被取消时调用，幂等无害）。"""
        try:
            self.append(stream_id, {"type": _END_EVENT_TYPE, "error": error})
        except Exception as e:  # 终局标记写失败不抛——重连方按「无新事件+不活跃」收尾
            logger.warning(f"事件日志终局标记写入失败: {e}")

    # ------------------------------------------------------------------
    # 读取（重连接口）
    # ------------------------------------------------------------------

    def read_after(self, stream_id: str, after_seq: int) -> list[dict[str, Any]]:
        """返回 seq > after_seq 的事件（保持顺序，payload 自带 seq 字段）。"""
        if self._client is not None:
            key = f"{_KEY_PREFIX}:{stream_id}:events"
            raw = self._client.lrange(key, 0, -1)
            events = [json.loads(r) for r in raw if r]
        else:
            with self._lock:
                entry = self._memory.get(stream_id)
                if entry is None:
                    return []
                events, expires_at = entry
                if time.monotonic() >= expires_at:
                    self._memory.pop(stream_id, None)
                    return []
                events = list(events)
        return [e for e in events if int(e.get("seq", 0)) > int(after_seq)]

    def exists(self, stream_id: str) -> bool:
        """该流是否存在事件日志（未知的 request_id → 404）。"""
        if self._client is not None:
            return bool(self._client.exists(f"{_KEY_PREFIX}:{stream_id}:events"))
        with self._lock:
            entry = self._memory.get(stream_id)
            if entry is None:
                return False
            return time.monotonic() < entry[1]

    # ------------------------------------------------------------------

    def _memory_get_or_create(self, stream_id: str) -> tuple[list[dict[str, Any]], float]:
        entry = self._memory.get(stream_id)
        if entry is None or time.monotonic() >= entry[1]:
            entry = ([], time.monotonic() + self._ttl)
            self._memory[stream_id] = entry
        return entry


# ---------------------------------------------------------------------------
# 全局单例（bridge 写入与 resume 读取共用同一实例）
# ---------------------------------------------------------------------------

_log: "StreamEventLog | None" = None
_log_lock = threading.Lock()


def get_stream_log() -> StreamEventLog:
    global _log
    if _log is None:
        with _log_lock:
            if _log is None:
                from src.config import REDIS_URL, STREAM_LOG_TTL_SECONDS

                _log = StreamEventLog(redis_url=REDIS_URL, ttl_seconds=STREAM_LOG_TTL_SECONDS)
    return _log


def reset_stream_log() -> None:
    """重置全局单例（测试 monkeypatch 配置后调用）。"""
    global _log
    with _log_lock:
        _log = None
