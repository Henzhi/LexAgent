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
- **原子预占（2026-09-03）**：付费调用发起前用 Lua 脚本原子预占配额
  （INCRBY→比较→超限回滚），失败则归还。只读的 check + 事后 record 存在
  TOCTOU 窗口，并发下日限额会被放大；预占是唯一能在**花钱之前**拦截的位置。

用法:
    budget = get_budget()
    budget.check("llm")                  # 只读前置拦截（入口，不占配额）
    budget.check_and_reserve("llm")      # 付费调用前：原子预占，超限抛异常
    budget.release("llm")                # 调用失败：归还预占
    budget.status()                      # 供健康检查/运维接口查询
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

# ---------------------------------------------------------------------------
# Lua 脚本：把「检查 + 计数」压成一次原子往返
# ---------------------------------------------------------------------------
# 背景（2026-09-03 审查整改）：原先 check() 是 GET、record() 是 INCRBY，两次独立
# 往返之间存在 TOCTOU 窗口——并发 N 个流可以同时通过 check，随后各自 record，
# 日限额实际被放大到 limit + (N-1)。
#
# 采用「预占（reserve）+ 归还（release）」模型闭合窗口：
#   付费调用发起前原子预占配额，调用失败则归还。这样：
#   - 并发下被拒的请求在**付费之前**就被拦下（拿到友好提示，而不是答案生成到一半被截断）
#   - 已用量统计不会少记（预占即计数），也不会多记（失败归还）
#
# ⚠️ 为什么不用「record() 时才发现超限」：那时钱已经花出去了，抛异常会截断正在生成的
# 回答，不抛又会突破限额——两头都不对。预占是唯一能在付费前拦截的位置。

# 预占：INCRBY → 超限则（enforce 时）回滚并返回 -1，否则返回预占后的值。
# enforce=0（BUDGET_ENFORCE=false 观察期）不回滚：调用确实发生了，必须照实计数，
# 只是不拦截——与 check() 的"只告警不拦截"语义保持一致。
_LUA_RESERVE = """
local key     = KEYS[1]
local limit   = tonumber(ARGV[1])
local n       = tonumber(ARGV[2])
local ttl     = tonumber(ARGV[3])
local enforce = tonumber(ARGV[4])
local cur     = redis.call('INCRBY', key, n)
if limit > 0 and cur > limit then
    if enforce == 1 then
        redis.call('DECRBY', key, n)
        return -1
    end
    if ttl > 0 then
        redis.call('EXPIRE', key, ttl)
    end
    return cur
end
if ttl > 0 then
    redis.call('EXPIRE', key, ttl)
end
return cur
"""

# 归还：DECRBY 并兜底不为负（调用失败/流中断时释放预占）
_LUA_RELEASE = """
local key = KEYS[1]
local n   = tonumber(ARGV[1])
local cur = tonumber(redis.call('GET', key) or '0')
if cur <= 0 then
    return 0
end
local v = cur - n
if v < 0 then
    v = 0
end
redis.call('SET', key, v)
return v
"""


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
        # Lua 脚本对象缓存（register_script 每次都会重新编译，按需缓存）
        self._scripts: dict[str, object] = {}
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

    def _alert_limit_reached(self, kind: str, used: int, limit: int) -> None:
        """超限时记录一条 ERROR 日志（同日同种类仅一次，避免刷屏）。"""
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

    def check(self, kind: str) -> None:
        """熔断检查（**只读**，不占用配额）：超限且 enforce=True 时抛异常。

        用途：请求入口的前置拦截（`_budget_block_message`）——此时还没确定
        会不会真的调用付费 API，不该占用配额。

        ⚠️ 真正要发起付费调用前请用 `check_and_reserve()`：只读的 check 与
        随后的 record 之间存在并发窗口（TOCTOU），只有原子预占能闭合它。
        """
        limit = self.limit_of(kind)
        if limit <= 0:
            return
        used = self.used(kind)
        if used < limit:
            return

        self._alert_limit_reached(kind, used, limit)
        if self._enforce:
            raise BudgetExceededError(kind, used, limit)

    # ------------------------------------------------------------------
    # 原子预占 / 归还（F14 TOCTOU 整改）
    # ------------------------------------------------------------------

    def reserve(self, kind: str, n: int = 1) -> bool:
        """原子预占 n 个配额。成功 True；超限且 enforce 时回滚并返回 False。

        `BUDGET_ENFORCE=false`（观察期）下超限**不回滚也返回 True**——调用确实
        发生了，必须照实计数，只是不拦截，与 check() 的语义一致。

        Redis 路径走 Lua 脚本（单次往返，INCRBY→比较→超限 DECRBY 回滚）；
        Redis 不可用或脚本执行失败时退化为进程内加锁预占——**统计/存储故障
        不拖垮主链路，但熔断能力保留**（与 Redis 降级语义一致）。
        """
        if kind not in _KINDS or n <= 0:
            return True
        day = self._today()
        if self._client is not None:
            try:
                script = self._reserve_script()
                limit = self.limit_of(kind)
                new_val = int(
                    script(
                        keys=[self._key(day, kind)],
                        args=[limit, n, self._seconds_until_tomorrow(), 1 if self._enforce else 0],
                    )
                )
                if new_val < 0:
                    self._alert_limit_reached(kind, self.used(kind), limit)
                    return False
                if limit > 0 and new_val > limit:
                    # enforce=false 的观察期：已照实计数，仅告警放行
                    self._alert_limit_reached(kind, new_val, limit)
                return True
            except Exception as e:
                logger.warning(f"预算预占（Lua）失败，退化为非原子 Redis 路径: {e}")
            # 二级兜底：非原子的 INCRBY→比较→回滚。它**没有**并发安全性，
            # 但换来了存储一致性——若此时退回进程内计数，used() 仍读 Redis，
            # 两边数据不一致会让熔断彻底失效。Lua 失败本就是异常态，
            # 宁可失去并发安全也不能失去一致性。
            try:
                key = self._key(day, kind)
                limit = self.limit_of(kind)
                new_val = int(self._client.incrby(key, n))
                if limit > 0 and new_val > limit:
                    if self._enforce:
                        self._client.decrby(key, n)
                        self._alert_limit_reached(kind, self.used(kind), limit)
                        return False
                    self._alert_limit_reached(kind, new_val, limit)
                self._client.expire(key, self._seconds_until_tomorrow())
                return True
            except Exception as e:
                logger.warning(f"预算预占（Redis）失败，退化为进程内计数: {e}")
        return self._reserve_memory(day, kind, n)

    def _reserve_script(self):
        """取（并缓存）预占 Lua 脚本对象。"""
        script = self._scripts.get("reserve")
        if script is None:
            script = self._client.register_script(_LUA_RESERVE)
            self._scripts["reserve"] = script
        return script

    def _reserve_memory(self, day: str, kind: str, n: int) -> bool:
        """进程内预占（Redis 不可用时的兜底，加锁保证原子）。"""
        with self._lock:
            counts = self._memory_for(day)
            limit = self.limit_of(kind)
            new_val = counts.get(kind, 0) + n
            if limit > 0 and new_val > limit:
                if self._enforce:
                    self._alert_limit_reached(kind, counts.get(kind, 0), limit)
                    return False
                # 观察期：照实计数，仅告警放行
                counts[kind] = new_val
                self._alert_limit_reached(kind, new_val, limit)
                return True
            counts[kind] = new_val
            return True

    def release(self, kind: str, n: int = 1) -> None:
        """归还预占的配额（调用失败 / 流中断时），计数兜底不为负。

        任何异常都不向上传播——归还失败只会导致配额少一点，不应影响主链路。
        """
        if kind not in _KINDS or n <= 0:
            return
        day = self._today()
        if self._client is not None:
            try:
                script = self._release_script()
                script(keys=[self._key(day, kind)], args=[n])
                return
            except Exception as e:
                logger.warning(f"预算归还（Lua）失败，退化为非原子 Redis 路径: {e}")
            try:
                # 与 reserve 的二级兜底对称：保持存储一致性优先
                key = self._key(day, kind)
                if int(self._client.decrby(key, n)) < 0:
                    self._client.set(key, 0)
                return
            except Exception as e:
                logger.warning(f"预算归还（Redis）失败，退化为进程内计数: {e}")
        with self._lock:
            counts = self._memory_for(day)
            counts[kind] = max(0, counts.get(kind, 0) - n)

    def _release_script(self):
        """取（并缓存）归还 Lua 脚本对象。"""
        script = self._scripts.get("release")
        if script is None:
            script = self._client.register_script(_LUA_RELEASE)
            self._scripts["release"] = script
        return script

    def check_and_reserve(self, kind: str, n: int = 1) -> None:
        """付费调用前的标准闸门：原子预占，超限抛 BudgetExceededError。

        与 `check()` 的区别：check 只看不占（入口前置拦截用），本方法**占用**
        配额且不存并发窗口。调用失败时须由调用方 `release()` 归还。
        """
        if self.limit_of(kind) <= 0:
            return
        if not self.reserve(kind, n):
            raise BudgetExceededError(kind, self.used(kind), self.limit_of(kind))

    def record(self, kind: str, n: int = 1) -> None:
        """直接累加用量——**无并发保护，仅用于没有预占场景的手工/补记**。

        ⚠️ 付费调用路径请改用 `check_and_reserve()` + `release()`：本方法与
        `check()` 组合存在 TOCTOU 窗口（并发 N 个流可同时通过 check 后各自
        累加，日限额被放大到 limit + N-1）。

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
