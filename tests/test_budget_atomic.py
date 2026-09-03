"""F14 预算计数原子性守护测试（2026-09-03 审查整改）。

背景：原先 `check()`（GET）与 `record()`（INCRBY）是两次独立往返，之间存在
TOCTOU 窗口——并发 N 个流可同时通过 check，随后各自 record，日限额被放大到
limit + (N-1)。整改后改为「原子预占（reserve）+ 失败归还（release）」。

本文件的核心断言：**并发预占下，成功的预占次数恰好等于 limit，一次不多**。
这类守护测试的意义在于——改回非原子实现会立刻转红，而不是默默多花钱。
"""

from __future__ import annotations

import threading

import pytest

from src.observability.cost_budget import (
    KIND_LLM,
    KIND_TAVILY,
    BudgetExceededError,
    CostBudget,
    reset_budget,
)


class _FakeScript:
    """模拟 redis-py 的 Script 对象：__call__(keys=[...], args=[...])。"""

    def __init__(self, store: dict, lua: str):
        self._store = store
        self._lua = lua

    def __call__(self, keys=None, args=None, **_kwargs):
        key = keys[0]
        if "DECRBY" in self._lua and "INCRBY" in self._lua:
            # reserve：INCRBY → 超限则（enforce 时）回滚
            limit = int(args[0])
            n = int(args[1])
            enforce = int(args[3])
            cur = self._store.get(key, 0) + n
            if limit > 0 and cur > limit:
                if enforce == 1:
                    return -1
                self._store[key] = cur  # 观察期：照实计数
                return cur
            self._store[key] = cur
            return cur
        # release：DECRBY 兜底不为负
        n = int(args[0])
        cur = self._store.get(key, 0)
        if cur <= 0:
            return 0
        self._store[key] = max(0, cur - n)
        return self._store[key]


class _FakeRedis:
    """最小 Redis 替身：只实现预算模块用到的 get / register_script / delete。"""

    def __init__(self):
        self.store: dict[str, int] = {}

    def get(self, key):
        return self._store_str(key)

    def _store_str(self, key):
        v = self.store.get(key)
        return None if v is None else str(v)

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def incrby(self, key, n):
        self.store[key] = self.store.get(key, 0) + n
        return self.store[key]

    def decrby(self, key, n):
        self.store[key] = self.store.get(key, 0) - n
        return self.store[key]

    def set(self, key, value):
        self.store[key] = int(value)

    def expire(self, key, ttl):  # noqa: ARG002
        return True

    def register_script(self, lua: str):
        return _FakeScript(self.store, lua)


@pytest.fixture
def redis_budget():
    """走（假）Redis 路径的预算实例。"""
    b = CostBudget(redis_url="", limits={KIND_LLM: 5, KIND_TAVILY: 2}, enforce=True)
    fake = _FakeRedis()
    object.__setattr__(b, "_client", fake)
    b.reset()
    return b


@pytest.fixture
def mem_budget():
    """纯内存路径的预算实例。"""
    b = CostBudget(redis_url="", limits={KIND_LLM: 5, KIND_TAVILY: 2}, enforce=True)
    b.reset()
    return b


class TestReserveSemantics:
    def test_reserve_returns_true_until_limit(self, redis_budget):
        for _ in range(5):
            assert redis_budget.reserve(KIND_LLM) is True
        assert redis_budget.reserve(KIND_LLM) is False

    def test_rejected_reserve_does_not_change_used(self, redis_budget):
        """被拒的预占必须回滚——不能把计数顶上去。"""
        for _ in range(5):
            redis_budget.reserve(KIND_LLM)
        assert redis_budget.used(KIND_LLM) == 5
        assert redis_budget.reserve(KIND_LLM) is False
        assert redis_budget.used(KIND_LLM) == 5, "被拒的预占必须回滚，计数不应变化"

    def test_release_frees_quota(self, redis_budget):
        redis_budget.reserve(KIND_LLM)
        redis_budget.release(KIND_LLM)
        assert redis_budget.used(KIND_LLM) == 0
        assert redis_budget.reserve(KIND_LLM) is True

    def test_release_never_goes_negative(self, redis_budget):
        for _ in range(3):
            redis_budget.release(KIND_LLM)
        assert redis_budget.used(KIND_LLM) == 0

    def test_check_and_reserve_raises_at_limit(self, redis_budget):
        for _ in range(5):
            redis_budget.check_and_reserve(KIND_LLM)
        with pytest.raises(BudgetExceededError):
            redis_budget.check_and_reserve(KIND_LLM)

    def test_unlimited_kind_never_rejects(self, redis_budget):
        """limit=0（不限制）时预占永不拒绝。"""
        redis_budget._limits[KIND_LLM] = 0
        for _ in range(50):
            assert redis_budget.reserve(KIND_LLM) is True

    def test_enforce_false_counts_but_never_blocks(self):
        """BUDGET_ENFORCE=false（观察期）：超限时照实计数，但不拦截。"""
        b = CostBudget(redis_url="", limits={KIND_LLM: 2}, enforce=False)
        b.reset()
        results = [b.reserve(KIND_LLM) for _ in range(5)]
        assert results == [True] * 5, "观察期不应拦截任何调用"
        assert b.used(KIND_LLM) == 5, "观察期仍须照实计数，否则拿不到真实用量基线"

    def test_check_is_readonly(self, redis_budget):
        """check() 只读：不改变已用量（入口前置拦截依赖这个语义）。"""
        redis_budget.check(KIND_LLM)
        assert redis_budget.used(KIND_LLM) == 0


class TestConcurrentAtomicity:
    """核心：并发预占不得突破限额（改回非原子实现会转红）。"""

    def _run(self, budget, kind, n_threads, limit):
        granted = []
        lock = threading.Lock()
        start = threading.Barrier(n_threads)

        def worker():
            start.wait()
            if budget.reserve(kind):
                with lock:
                    granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(granted) == limit, f"并发预占成功数应恰好等于 limit={limit}，实际 {len(granted)}"
        assert budget.used(kind) == limit

    def test_redis_path_concurrent_reserve_bounded_by_limit(self, redis_budget):
        self._run(redis_budget, KIND_LLM, n_threads=40, limit=5)

    def test_memory_path_concurrent_reserve_bounded_by_limit(self, mem_budget):
        """Redis 不可用退化到进程内时同样不能超发（加锁保证）。"""
        self._run(mem_budget, KIND_LLM, n_threads=40, limit=5)

    def test_memory_path_concurrent_tavily(self, mem_budget):
        self._run(mem_budget, KIND_TAVILY, n_threads=30, limit=2)


class TestLuaFailureDegrades:
    def test_lua_failure_preserves_store_consistency(self, redis_budget):
        """Lua 失败 → 退化为非原子 Redis 路径，**计数必须仍落在 Redis**。

        这里是刻意的设计取舍：若退回进程内计数，则 reserve 写内存、used() 读
        Redis，两边不一致 → check/reserve 永远看到 0，熔断彻底失效。宁可失去
        并发安全性，也不能失去存储一致性（Lua 失败本就是异常态）。
        """

        class _BrokenClient(_FakeRedis):
            def register_script(self, lua):  # noqa: ARG002
                raise RuntimeError("script load failed")

        broken = _BrokenClient()
        object.__setattr__(redis_budget, "_client", broken)
        redis_budget._scripts.clear()

        for _ in range(5):
            assert redis_budget.reserve(KIND_LLM) is True
        assert redis_budget.reserve(KIND_LLM) is False
        assert redis_budget.used(KIND_LLM) == 5, "回退后计数必须仍在 Redis 上，否则熔断失效"

    def test_redis_totally_down_falls_back_to_memory(self):
        """Redis 整体不可用 → 进程内预占照常工作（used() 也走内存，两边一致）。"""
        b = CostBudget(redis_url="redis://127.0.0.1:1/0", limits={KIND_LLM: 2}, enforce=True)
        b.reset()
        assert b.reserve(KIND_LLM) is True
        assert b.reserve(KIND_LLM) is True
        assert b.reserve(KIND_LLM) is False
        assert b.used(KIND_LLM) == 2

    def test_broken_client_falls_back_to_memory(self):
        """Redis 客户端每个操作都抛异常 → 退化为进程内计数，不崩、不阻断主链路。"""
        b = CostBudget(redis_url="", limits={KIND_LLM: 3}, enforce=True)
        b.reset()

        class _Broken:
            def __getattr__(self, _name):
                raise RuntimeError("redis down")

        object.__setattr__(b, "_client", _Broken())
        b._scripts.clear()
        for _ in range(3):
            assert b.reserve(KIND_LLM) is True
        assert b.reserve(KIND_LLM) is False


class TestBudgetCallbackIntegration:
    """预算 callback 改用预占后的语义（重复计数会把限额腰斩，必须守住）。"""

    def _setup(self, monkeypatch, limit: int):
        monkeypatch.setattr("src.observability.cost_budget._budget", None)
        monkeypatch.setattr("src.config.BUDGET_ENABLED", True)
        monkeypatch.setattr("src.config.BUDGET_MAX_LLM_CALLS_PER_DAY", limit)
        reset_budget()
        from src.observability.cost_budget import get_budget

        b = get_budget()
        b.reset(KIND_LLM)
        return b

    def test_success_consumes_exactly_one(self, monkeypatch):
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        b = self._setup(monkeypatch, 10)
        h = LLMBudgetCallbackHandler()
        h.on_llm_start({}, ["hi"])
        h.on_llm_end(None)
        assert b.used(KIND_LLM) == 1, "成功后只应占 1 个配额（预占 1 次，on_llm_end 不得重复计数）"
        reset_budget()

    def test_error_refunds_reservation(self, monkeypatch):
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        b = self._setup(monkeypatch, 10)
        h = LLMBudgetCallbackHandler()
        h.on_llm_start({}, ["hi"])
        assert b.used(KIND_LLM) == 1
        h.on_llm_error(RuntimeError("boom"))
        assert b.used(KIND_LLM) == 0, "调用失败必须归还预占的配额"
        reset_budget()

    def test_exceeded_raises_on_start(self, monkeypatch):
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        self._setup(monkeypatch, 1)
        h = LLMBudgetCallbackHandler()
        h.on_llm_start({}, ["hi"])
        h.on_llm_end(None)
        with pytest.raises(BudgetExceededError):
            h.on_llm_start({}, ["再问一次"])
        reset_budget()

    def test_20_calls_consume_20_not_40(self, monkeypatch):
        """回归：一次请求 18~20 次 LLM 调用，配额消耗必须等于调用次数。"""
        from src.llm.budget_callback import LLMBudgetCallbackHandler

        b = self._setup(monkeypatch, 100)
        h = LLMBudgetCallbackHandler()
        for _ in range(20):
            h.on_llm_start({}, ["hi"])
            h.on_llm_end(None)
        assert b.used(KIND_LLM) == 20
        reset_budget()
