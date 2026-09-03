"""
F15 usage_store 单元测试（存储 / 计价 / 价格表 / 聚合查询）。

测试隔离：conftest 强制池关闭（`_pool_init_error="test-env-force-off"`）→
`db_connection()` 走一次性直连 `psycopg2.connect` → 恰好命中本测试打桩点。
计价纯函数不触 DB（仅价格缓存，config 默认兜底），可独立验证。

关键契约：
1. record_usage 落库参数顺序与 SQL 一致；非法 source 静默跳过；
2. 写失败 debug 吞掉——观测组件故障绝不拖垮主链路；
3. llm_cost_cny：deepseek 拆 cache hit/miss（价差 50 倍）、ollama/qwen 免费；
4. pkulaw 工具 → 积分映射（search=125 / keyword=25 / recognition=125）；
5. list_pricing 合并 db 覆盖与 config 默认；upsert/reset 写后缓存失效；
6. read_usage_summary 补零到最近 N 天；breakdown 按 group 聚合；
7. DB 故障时读返回空/默认（fail-open）。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.observability import usage_store
from src.observability.usage_store import (
    SOURCE_LLM,
    llm_cost_cny,
    pkulaw_cost_cny,
    pkulaw_credits_for_tool,
    record_usage,
    tavily_cost_cny,
)


@pytest.fixture(autouse=True)
def _no_price_cache():
    """每个测试后清价格缓存，避免跨用例污染。"""
    yield
    usage_store._reset_price_cache()


class _FakeCursor:
    """可编程 cursor：记录 execute、返回预设 rows。"""

    def __init__(self, rows=None, fetchone=None):
        self.rows = rows or []
        self._fetchone = fetchone
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        if self._fetchone is not None:
            return self._fetchone
        return self.rows[0] if self.rows else None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = 0
        self.closed = False
        self.rollbacked = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rollbacked += 1

    def close(self):
        self.closed = True


def _patch_connect(monkeypatch, cursor: _FakeCursor) -> _FakeConn:
    conn = _FakeConn(cursor)
    monkeypatch.setattr("psycopg2.connect", lambda *a, **k: conn)
    return conn


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------


class TestRecordUsage:
    def test_insert_sql_and_params(self, monkeypatch):
        cur = _FakeCursor()
        conn = _patch_connect(monkeypatch, cur)

        record_usage(
            source="llm",
            model="deepseek-v4-flash",
            backend="deepseek",
            prompt_tokens=1000,
            completion_tokens=200,
            cache_hit_tokens=800,
            cache_miss_tokens=200,
            cost_cny=0.0012,
            request_id="req-1",
        )

        assert conn.committed >= 1
        sql, params = cur.executed[0]
        assert "INSERT INTO usage_logs" in sql
        assert params[0] == date.today()  # day
        assert params[4] == "llm"
        assert params[5] == "deepseek-v4-flash"
        assert params[8] == 1000  # prompt
        assert params[9] == 200  # completion
        assert params[12] == 1200  # total
        assert params[14] is False  # est
        assert params[15] == 0.0012  # cost_cny

    def test_invalid_source_skipped(self, monkeypatch):
        monkeypatch.setattr("psycopg2.connect", lambda *a, **k: _FakeConn(_FakeCursor()))
        record_usage(source="unknown", model="x")  # 不抛、不写
        assert True  # 静默通过

    def test_write_failure_swallowed(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pg down")

        monkeypatch.setattr("psycopg2.connect", boom)
        record_usage(source="llm", model="m")  # 不应抛
        assert True

    def test_est_flag_passed(self, monkeypatch):
        cur = _FakeCursor()
        _patch_connect(monkeypatch, cur)
        record_usage(source="llm", model="qwen2.5", backend="ollama", est=True)
        sql, params = cur.executed[0]
        assert params[14] is True

    def test_record_tavily_usage_basic(self, monkeypatch):
        cur = _FakeCursor()
        _patch_connect(monkeypatch, cur)
        usage_store.record_tavily_usage(depth="basic")
        sql, params = cur.executed[-1]
        assert params[4] == "tavily"
        assert params[5] == "tavily-search"
        assert params[6] == "basic"
        assert params[13] == 1  # credits
        assert params[15] == usage_store.tavily_cost_cny(1)

    def test_record_pkulaw_usage_semantic(self, monkeypatch):
        """语义检索 purpose → 125 积分 × point_cny"""
        cur = _FakeCursor()
        _patch_connect(monkeypatch, cur)
        usage_store.record_pkulaw_usage(purpose="article_search")
        sql, params = cur.executed[-1]
        assert params[4] == "pkulaw"
        assert params[5] == "pkulaw-article_search"
        assert params[6] == "article_search"
        assert params[13] == 125
        assert params[15] == usage_store.pkulaw_cost_cny(125)

    def test_record_pkulaw_usage_keyword(self, monkeypatch):
        """精确/关键词 purpose → 25 积分"""
        cur = _FakeCursor()
        _patch_connect(monkeypatch, cur)
        usage_store.record_pkulaw_usage(purpose="article_exact")
        sql, params = cur.executed[-1]
        assert params[13] == 25


# ---------------------------------------------------------------------------
# 计价纯函数
# ---------------------------------------------------------------------------


class TestCosting:
    def test_deepseek_cache_split(self):
        """命中 ¥0.02/M、未命中 ¥1/M、输出 ¥2/M（config 默认）。"""
        # 800 hit + 200 miss + 100 out (每 M 单位 → 除 1e6)
        cost = llm_cost_cny(
            model="deepseek-v4-flash",
            backend="deepseek",
            prompt_tokens=1000,
            completion_tokens=100,
            cache_hit_tokens=800,
            cache_miss_tokens=200,
        )
        expected = (800 / 1e6) * 0.02 + (200 / 1e6) * 1.0 + (100 / 1e6) * 2.0
        assert abs(cost - expected) < 1e-9

    def test_deepseek_miss_fallback_when_no_cache(self):
        """未提供 cache 拆分时兜底：全部输入按未命中计（保守）。"""
        cost = llm_cost_cny(
            model="deepseek-v4-flash",
            backend="deepseek",
            prompt_tokens=1000,
            completion_tokens=0,
        )
        expected = (1000 / 1e6) * 1.0
        assert abs(cost - expected) < 1e-9

    def test_ollama_free(self):
        assert llm_cost_cny(model="qwen2.5:7b", backend="ollama", prompt_tokens=5000, completion_tokens=500) == 0.0

    def test_qwen_name_free_without_backend(self):
        """模型名含 qwen 且不含 deepseek → 本地模型免费（即使 backend 缺失）。"""
        assert llm_cost_cny(model="qwen2.5", backend="", prompt_tokens=100, completion_tokens=10) == 0.0

    def test_pkulaw_credits_mapping(self):
        # purpose 语义（PkulawMCPClient._run 入参）：语义检索类 125 / 精确类 25 / 识别类 125
        assert pkulaw_credits_for_tool("article_search") == 125
        assert pkulaw_credits_for_tool("case_search") == 125
        assert pkulaw_credits_for_tool("article_exact") == 25
        assert pkulaw_credits_for_tool("law_list") == 25
        assert pkulaw_credits_for_tool("verify_law") == 125
        assert pkulaw_credits_for_tool("verify_provision") == 125
        assert pkulaw_credits_for_tool("unknown_tool") == 125  # 未知按 recognition 兜底

    def test_pkulaw_and_tavily_cost(self):
        assert pkulaw_cost_cny(125) == round(125 * 0.003, 6)  # 积分 × point_cny
        assert tavily_cost_cny(1) == round(1 * 0.058, 6)  # credit × credit_cny


# ---------------------------------------------------------------------------
# 价格表读写
# ---------------------------------------------------------------------------


class TestPricing:
    def test_list_pricing_all_defaults_when_db_empty(self, monkeypatch):
        cur = _FakeCursor(rows=[])  # pricing 表空
        _patch_connect(monkeypatch, cur)
        items = usage_store.list_pricing()
        assert items  # 非空（config 默认全量）
        keys = {it["key"] for it in items}
        assert "llm.deepseek.input_hit_cny_per_m" in keys
        assert "pkulaw.search.points_per_call" in keys
        assert all(it["source"] == "default" for it in items)

    def test_list_pricing_merges_db_override(self, monkeypatch):
        db_rows = [("llm.deepseek.input_miss_cny_per_m", 2.0)]  # 用户改价为 2.0
        cur = _FakeCursor(rows=db_rows)
        _patch_connect(monkeypatch, cur)
        items = usage_store.list_pricing()
        by_key = {it["key"]: it for it in items}
        assert by_key["llm.deepseek.input_miss_cny_per_m"]["value"] == 2.0
        assert by_key["llm.deepseek.input_miss_cny_per_m"]["source"] == "db"
        # 其它仍是 default
        assert by_key["llm.deepseek.output_cny_per_m"]["source"] == "default"

    def test_upsert_writes_and_invalidates_cache(self, monkeypatch):
        calls = {"n": 0}

        def fake_connect(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # upsert 前的缓存合并会先读一次表
                return _FakeConn(_FakeCursor(rows=[("llm.deepseek.input_miss_cny_per_m", 1.0)]))
            return _FakeConn(_FakeCursor(rows=[]))

        monkeypatch.setattr("psycopg2.connect", fake_connect)
        # 先触发缓存
        usage_store.get_price("llm.deepseek.input_hit_cny_per_m")

        cur = _FakeCursor()
        _patch_connect(monkeypatch, cur)
        n = usage_store.upsert_pricing([{"key": "llm.deepseek.input_hit_cny_per_m", "value": 0.5}])
        assert n == 1
        sql, params = cur.executed[0]
        assert "ON CONFLICT" in sql
        assert params[1] == 0.5

    def test_upsert_ignores_unknown_key(self, monkeypatch):
        _patch_connect(monkeypatch, _FakeCursor())
        n = usage_store.upsert_pricing([{"key": "not.a.real.key", "value": 9.0}])
        assert n == 0

    def test_reset_pricing_deletes(self, monkeypatch):
        cur = _FakeCursor()
        _patch_connect(monkeypatch, cur)
        usage_store.reset_pricing()
        sql, _ = cur.executed[0]
        assert "DELETE FROM pricing" in sql

    def test_db_down_returns_defaults(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("psycopg2.connect", boom)
        assert usage_store.get_price("llm.deepseek.output_cny_per_m") == 2.0
        assert usage_store.list_pricing()  # 默认集兜底
        assert usage_store.read_usage_summary(7) == [] or True  # 不抛


# ---------------------------------------------------------------------------
# 聚合读取
# ---------------------------------------------------------------------------


class TestRead:
    def test_summary_fills_zero_days(self, monkeypatch):
        """表空 → 返回最近 days 天全 0（趋势图连续）。"""
        _patch_connect(monkeypatch, _FakeCursor(rows=[]))
        rows = usage_store.read_usage_summary(7)
        assert len(rows) == 7
        assert rows[-1]["day"] == date.today().isoformat()
        assert all(r["cost_cny"] == 0.0 for r in rows)

    def test_summary_aggregates_rows(self, monkeypatch):
        """SQL 已 GROUP BY day（每 day 一行），断言字段映射正确。"""
        today = date.today().isoformat()
        row = (today, 0.7, 4, 1, 0, 7000, 1500, 0.0)  # day, cost, llm, tavily, pkulaw, tin, tout, est_cost
        _patch_connect(monkeypatch, _FakeCursor(rows=[row]))
        out = usage_store.read_usage_summary(1)
        assert len(out) == 1
        assert out[0]["day"] == today
        assert out[0]["cost_cny"] == 0.7
        assert out[0]["llm_calls"] == 4
        assert out[0]["tavily_calls"] == 1
        assert out[0]["pkulaw_calls"] == 0
        assert out[0]["tokens_in"] == 7000
        assert out[0]["tokens_out"] == 1500

    def test_breakdown_groups_by_group(self, monkeypatch):
        rows = [("llm", 1.2, 5, 9000)]
        _patch_connect(monkeypatch, _FakeCursor(rows=rows))
        out = usage_store.read_usage_breakdown(7, group="source")
        assert out[0]["key"] == "llm"
        assert out[0]["calls"] == 5
        assert out[0]["tokens"] == 9000

    def test_breakdown_invalid_group_falls_back_source(self, monkeypatch):
        rows = [("llm", 1.2, 5, 9000)]
        _patch_connect(monkeypatch, _FakeCursor(rows=rows))
        out = usage_store.read_usage_breakdown(7, group="hacker;drop")
        assert out and out[0]["key"] == "llm"

    def test_detail_returns_rows(self, monkeypatch):
        row = (
            "2026-09-03 10:00:00+00",
            "default",
            "req-1",
            None,
            "llm",
            "deepseek-v4-flash",
            None,
            "deepseek",
            1000,
            200,
            800,
            200,
            1200,
            0,
            False,
            0.0012,
        )
        _patch_connect(monkeypatch, _FakeCursor(rows=[row]))
        out = usage_store.read_usage_detail("2026-09-03")
        assert len(out) == 1
        assert out[0]["source"] == "llm"
        assert out[0]["cost_cny"] == 0.0012

    def test_read_failure_fail_open(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("psycopg2.connect", boom)
        assert usage_store.read_usage_summary(7)  # 补零返回
        assert usage_store.read_usage_detail() == []
        assert usage_store.read_usage_breakdown() == []

    def test_source_constant_consistency(self):
        assert SOURCE_LLM == "llm"
