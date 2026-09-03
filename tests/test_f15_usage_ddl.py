"""
F15 usage_logs / pricing 表 DDL 完整性测试。

docker/init.sql 由 PG 首次启动时执行，单测环境无法连真库验证语法；
本测试做**文件级守卫**：解析 init.sql 确认两表与关键列存在，
防止后续迭代误删列/表导致 usage_store 与 schema 失配（静默丢字段）。

真实建表以 docker compose up -d 时 PG 执行 init.sql 的结果为准。
"""

from __future__ import annotations

import re
from pathlib import Path

INIT_SQL = Path(__file__).resolve().parent.parent / "docker" / "init.sql"


def _sql_text() -> str:
    assert INIT_SQL.exists(), f"init.sql 不存在: {INIT_SQL}"
    return INIT_SQL.read_text(encoding="utf-8")


def _table_block(sql: str, table: str) -> str:
    """提取 CREATE TABLE IF NOT EXISTS {table} (...) 的括号内文本"""
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\);",
        sql,
        re.S | re.I,
    )
    assert m, f"init.sql 缺少 {table} 建表语句"
    return m.group(1)


class TestUsageLogsDdl:
    def test_table_exists(self):
        assert "CREATE TABLE IF NOT EXISTS usage_logs" in _sql_text()

    def test_key_columns_present(self):
        """usage_store.record_usage 落库需要的全部列都在（缺列 = 静默丢数据）"""
        block = _table_block(_sql_text(), "usage_logs")
        for col in [
            "id",
            "ts",
            "day",
            "user_id",
            "request_id",
            "session_id",
            "source",
            "model",
            "tool",
            "backend",
            "prompt_tokens",
            "completion_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "total_tokens",
            "credits",
            "est",
            "cost_cny",
            "created_at",
        ]:
            assert re.search(rf"\b{col}\s", block), f"usage_logs 缺列: {col}"

    def test_indexes_present(self):
        sql = _sql_text()
        for idx in ["idx_usage_logs_day", "idx_usage_logs_req", "idx_usage_logs_ts"]:
            assert f"CREATE INDEX IF NOT EXISTS {idx}" in sql, f"缺索引: {idx}"

    def test_comments_present(self):
        """列注释齐全（防文档漂移）"""
        assert "COMMENT ON TABLE usage_logs" in _sql_text()
        assert "COMMENT ON COLUMN usage_logs.cost_cny" in _sql_text()


class TestPricingDdl:
    def test_table_exists(self):
        assert "CREATE TABLE IF NOT EXISTS pricing" in _sql_text()

    def test_key_columns_present(self):
        block = _table_block(_sql_text(), "pricing")
        for col in ["key", "value", "unit", "note", "updated_at"]:
            assert re.search(rf"\b{col}\s", block), f"pricing 缺列: {col}"
