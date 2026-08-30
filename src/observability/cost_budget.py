"""
预算熔断（M3 / F14）：外部付费 API 的日用量统计与超限熔断。

监控对象：
- `llm`   ：DeepSeek（主）/ Ollama（降级）的**逻辑调用次数**
- `tavily`：网络搜索次数（Tavily 按次计费，口径精确）

设计要点：
- **计数口径**：LLM 按"逻辑调用次数"计（一次 `chat()` 内的 SDK 重试不重复计数），
  在 `LLMBackend` 的公开入口埋点，故不受各后端重试策略影响；Tavily 按次计。
  相比 token 口径，次数稳定可靠（流式响应拿不到 usage，token 只能估算）。
- **存储**：Redis 原子 `INCR` + TTL 到次日零点自动失效，跨进程共享。
- **降级**：Redis 不可用时自动退化为进程内计数并告警——**绝不因为监控
  组件故障导致主链路不可用**（与"工具失败不抛异常"同一原则）。
- **熔断粒度**：LLM 超限 → 无法生成回答，整体熔断；Tavily 超限 → 只停网络
  搜索，内部库与官方源仍可用，服务降级但可用（REQ-UW1 语义）。

用法:
    budget = get_budget()
    budget.check("llm")          # 超限抛 BudgetExceededError
    budget.record("llm")         # 成功后计数
    budget.status()              # 供健康检查/运维接口查询
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 用量种类
KIND_LLM = "llm"
KIND_TAVILY = "tavily"
KIND_PKULAW = "pkulaw"  # 北大法宝 MCP（按次计费，M3+ / F9 扩展）

_KINDS = (KIND_LLM, KIND_TAVILY, KIND_PKULAW)

# Redis key 前缀（与 FAQ 缓存共用同一 Redis 实例，前缀区分命名空间）
_KEY_PREFIX = "lexagent:budget"


class BudgetExceededError(RuntimeError):
    """预算超限异常。

    Attributes:
        kind: 超限的用量种类（llm / tavily）
        used: 当日已用量
        limit: 当日上限
    """

    def __init__(self, kind: str, used: int, limit: int):
        self.kind = kind
        self.used = used
        self.limit = limit
        label = "LLM 调用" if kind == KIND_LLM else "网络搜索" if kind == KIND_TAVILY else "北大法宝检索"
        super().__init__(f"{label}当日预算已用尽（{used}/{limit}），已暂停该能力；预算于次日零点自动重置。")


class CostBudget:
    """日用量统计与熔断。

    Args:
        redis_url: Redis 连接串；为空或连接失败时退化为进程内计数
        limits: {kind: 每日上限}，0 或缺失表示不限制
        enforce: True 时 check() 超限抛异常，False 时仅告警
    """

    def __init__(
        self,
        redis_url: str = "",
        limits: dict[str, int] | None = None,
        enforce: bool = True,
    ):
        self._limits: dict[str, int] = {k: max(0, int(v or 0)) for k, v in (limits or {}).items() if k in _KINDS}
        self._enforce = bool(enforce)
        self._client = None
        self._lock = threading.Lock()
        # Redis 不可用时的进程内兜底计数：{kind: 次数}（仅当天）
        self._memory: dict[str, int] = {}
        self._memory_date = ""
        # 告警去重：同一天同一 kind 只打一次 ERROR，避免刷屏
        self._alerted: set[tuple[str, str]] = set()

        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url, decode_responses=True)
                client.ping()
                self._client = client
                logger.info("预算统计启用 Redis 存储")
            except Exception as e:
                self._client = None
                logger.warning(f"Redis 不可用，预算统计退化为进程内计数: {e}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _seconds_until_tomorrow() -> int:
        """距次日零点的秒数（Redis TTL，跨天自动失效）。"""
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(60, int((tomorrow - now).total_seconds()))

    def _key(self, day: str, kind: str) -> str:
        return f"{_KEY_PREFIX}:{day}:{kind}"

    def _memory_for(self, day: str) -> dict[str, int]:
        """取当天的进程内计数（跨天自动丢弃旧数据）。"""
        if self._memory_date != day:
            self._memory = {}
            self._memory_date = day
        return self._memory

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def limit_of(self, kind: str) -> int:
        """该种类的每日上限（0 = 不限制）。"""
        return self._limits.get(kind, 0)

    def used(self, kind: str) -> int:
        """当日已用次数（Redis 不可用或出错时读进程内计数）。"""
        day = self._today()
        if self._client is not None:
            try:
                val = self._client.get(self._key(day, kind))
                return int(val or 0)
            except Exception as e:
                logger.warning(f"读取预算计数失败，回退进程内计数: {e}")
        return self._memory_for(day).get(kind, 0)

    def remaining(self, kind: str) -> int:
        """当日剩余可用次数（-1 表示不限制）。"""
        limit = self.limit_of(kind)
        if limit <= 0:
            return -1
        return max(0, limit - self.used(kind))

    def is_exceeded(self, kind: str) -> bool:
        """当日用量是否已达上限。"""
        limit = self.limit_of(kind)
        if limit <= 0:
            return False
        return self.used(kind) >= limit

    def check(self, kind: str) -> None:
        """熔断检查：超限且 enforce=True 时抛 BudgetExceededError。

        无论是否拦截，超限都会记录一条 ERROR 日志（同日同种类仅一次）。
        """
        limit = self.limit_of(kind)
        if limit <= 0:
            return
        used = self.used(kind)
        if used < limit:
            return

        day = self._today()
        if (day, kind) not in self._alerted:
            self._alerted.add((day, kind))
            logger.error(
                "[预算熔断] %s 当日用量已达上限 %d/%d，后续调用将%s",
                kind,
                used,
                limit,
                "被拦截" if self._enforce else "被放行（BUDGET_ENFORCE=false）",
            )
        if self._enforce:
            raise BudgetExceededError(kind, used, limit)

    def record(self, kind: str, n: int = 1) -> None:
        """累加用量（成功调用后调用）。

        Redis 不可用时写入进程内计数；任何异常都不向上传播——统计失败
        不应影响主链路。
        """
        if kind not in _KINDS or n <= 0:
            return
        day = self._today()
        if self._client is not None:
            try:
                key = self._key(day, kind)
                pipe = self._client.pipeline()
                pipe.incrby(key, n)
                pipe.expire(key, self._seconds_until_tomorrow())
                pipe.execute()
                return
            except Exception as e:
                logger.warning(f"写入预算计数失败，回退进程内计数: {e}")
        with self._lock:
            counts = self._memory_for(day)
            counts[kind] = counts.get(kind, 0) + n

    def reset(self, kind: str | None = None) -> None:
        """手动清零当日用量（运维用；自然重置靠 Redis TTL 跨天失效）。"""
        day = self._today()
        for k in (kind,) if kind else _KINDS:
            with self._lock:
                self._memory_for(day).pop(k, None)
            if self._client is not None:
                try:
                    self._client.delete(self._key(day, k))
                except Exception as e:
                    logger.warning(f"重置预算计数失败: {e}")
            self._alerted.discard((day, k))

    def status(self) -> dict:
        """当前预算状态（供健康检查 / 运维接口）。"""
        detail = {}
        for kind in _KINDS:
            limit = self.limit_of(kind)
            used = self.used(kind)
            detail[kind] = {
                "used": used,
                "limit": limit,
                "remaining": self.remaining(kind),
                "exceeded": self.is_exceeded(kind),
            }
        return {
            "enabled": True,
            "enforce": self._enforce,
            "date": self._today(),
            "storage": "redis" if self._client is not None else "memory",
            "exceeded": any(d["exceeded"] for d in detail.values()),
            "detail": detail,
        }


# ---------------------------------------------------------------------------
# 全局单例（懒加载，供各埋点位置取用）
# ---------------------------------------------------------------------------

_budget: "CostBudget | None" = None
_budget_lock = threading.Lock()


def get_budget() -> CostBudget:
    """获取全局 CostBudget 单例（按 src.config 当前配置构建）。

    测试 monkeypatch 配置后需调用 `reset_budget()` 重建。
    """
    global _budget
    if _budget is None:
        with _budget_lock:
            if _budget is None:
                from src.config import (
                    BUDGET_ENABLED,
                    BUDGET_ENFORCE,
                    BUDGET_MAX_LLM_CALLS_PER_DAY,
                    BUDGET_MAX_PKULAW_CALLS_PER_DAY,
                    BUDGET_MAX_TAVILY_CALLS_PER_DAY,
                    REDIS_URL,
                )

                limits = {}
                if BUDGET_ENABLED:
                    limits = {
                        KIND_LLM: BUDGET_MAX_LLM_CALLS_PER_DAY,
                        KIND_TAVILY: BUDGET_MAX_TAVILY_CALLS_PER_DAY,
                        KIND_PKULAW: BUDGET_MAX_PKULAW_CALLS_PER_DAY,
                    }
                _budget = CostBudget(
                    redis_url=REDIS_URL if BUDGET_ENABLED else "",
                    limits=limits,
                    enforce=BUDGET_ENFORCE,
                )
    return _budget


def reset_budget() -> None:
    """重置全局单例（测试 monkeypatch 配置后调用）。"""
    global _budget
    with _budget_lock:
        _budget = None
