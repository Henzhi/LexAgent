"""T-05：跨模块数据结构契约（Decision / CacheMeta / TelemetryEvent）。

这些结构是 T-09~T-12 的公共语言，字段名一旦漂移，后面的票会一起返工 ——
所以这里把字段名、类型、时区格式都**断言死**，而不是靠人眼看。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kv_cache import CacheMeta, Decision, DecisionKind, MissReason, TelemetryEvent, now_iso

SPEC_META_FIELDS = ("key", "model_id", "quant", "n_tokens", "bytes", "created_at", "last_used_at", "hits")


def make_meta(**overrides) -> CacheMeta:
    params = {
        "key": "a3f5b2c9d8e1f7a6",
        "model_id": "qwen2.5-3b-instruct",
        "quant": "Q4_K_M",
        "n_tokens": 4128,
        "bytes": 12_345_678,
        "created_at": "2026-09-11T21:00:00+08:00",
        "last_used_at": "2026-09-11T21:08:00+08:00",
        "hits": 3,
    }
    params.update(overrides)
    return CacheMeta(**params)


class TestTimestampHelper:
    def test_now_iso_carries_timezone_offset(self):
        value = now_iso()
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None

    def test_now_iso_matches_spec_shape(self):
        """形如 2026-09-11T21:00:00+08:00 —— 秒级精度 + 偏移。"""
        assert len(now_iso()) == len("2026-09-11T21:00:00+08:00")


class TestCacheMeta:
    def test_field_names_match_spec_4_3(self):
        """T-08 验收点名：meta schema 必须与 SPEC §4.3 逐字段一致。"""
        assert tuple(make_meta().to_dict()) == SPEC_META_FIELDS

    def test_roundtrip(self):
        meta = make_meta()
        assert CacheMeta.from_dict(meta.to_dict()) == meta

    @pytest.mark.parametrize("missing", SPEC_META_FIELDS[:-1])
    def test_from_dict_rejects_missing_field(self, missing):
        data = make_meta().to_dict()
        del data[missing]
        with pytest.raises(ValueError, match="缺少必填字段"):
            CacheMeta.from_dict(data)

    def test_from_dict_rejects_unknown_field(self):
        data = {**make_meta().to_dict(), "surprise": 1}
        with pytest.raises(ValueError, match="未知字段"):
            CacheMeta.from_dict(data)

    def test_hits_defaults_to_zero_when_absent(self):
        data = make_meta().to_dict()
        del data["hits"]
        assert CacheMeta.from_dict(data).hits == 0

    @pytest.mark.parametrize("field_name", ["created_at", "last_used_at"])
    def test_naive_timestamp_is_rejected(self, field_name):
        """丢时区的时间戳是隐性坑（T-08 验收点名），直接拒。"""
        with pytest.raises(ValueError, match="时区"):
            make_meta(**{field_name: "2026-09-11T21:00:00"})

    def test_illegal_timestamp_is_rejected(self):
        with pytest.raises(ValueError, match="ISO 8601"):
            make_meta(created_at="yesterday")

    def test_touched_advances_hits_and_last_used(self):
        meta = make_meta(hits=1, last_used_at="2026-09-11T21:08:00+08:00")
        bumped = meta.touched(at="2026-09-11T22:00:00+08:00")
        assert bumped.hits == 2
        assert bumped.last_used_at == "2026-09-11T22:00:00+08:00"
        assert meta.hits == 1, "原实例不可变（frozen dataclass）"

    def test_touched_keeps_created_at(self):
        """REQ-E1 的重复 save 语义：created_at 保留首次，不因命中而改写。"""
        meta = make_meta()
        assert meta.touched().created_at == meta.created_at


class TestDecision:
    def test_hit_factory(self):
        decision = Decision.hit_(key="k", filename="slotcache_k.bin")
        assert decision.kind is DecisionKind.HIT
        assert decision.hit is True
        assert decision.should_cold_prefill is False

    def test_miss_factory(self):
        decision = Decision.miss(MissReason.NOT_FOUND)
        assert decision.kind is DecisionKind.MISS
        assert decision.hit is False
        assert decision.should_cold_prefill is True

    def test_uncached_factory(self):
        decision = Decision.uncached(detail="server 未配 --slot-save-path")
        assert decision.kind is DecisionKind.UNCACHED
        assert decision.should_cold_prefill is True

    def test_hit_requires_key_and_filename(self):
        with pytest.raises(ValueError, match="HIT 必须带"):
            Decision(kind=DecisionKind.HIT)

    def test_hit_rejects_reason(self):
        with pytest.raises(ValueError, match="不应带 miss reason"):
            Decision(kind=DecisionKind.HIT, key="k", filename="f.bin", reason=MissReason.NOT_FOUND)

    def test_miss_requires_reason(self):
        """「失败不抛」不等于「失败静默」（T-09 备注）—— 无 reason 的 MISS 构造不出来。"""
        with pytest.raises(ValueError, match="MISS 必须带 reason"):
            Decision(kind=DecisionKind.MISS)

    def test_miss_rejects_filename(self):
        with pytest.raises(ValueError, match="MISS 不应带 filename"):
            Decision(kind=DecisionKind.MISS, reason=MissReason.NOT_FOUND, filename="f.bin")

    def test_both_non_hit_kinds_cold_prefill(self):
        for decision in (Decision.miss(MissReason.RESTORE_FAILED), Decision.uncached()):
            assert decision.should_cold_prefill is True


class TestMissReason:
    def test_covers_every_reason_required_by_t09(self):
        """T-09 验收点名的六个 reason，一个都不能少。"""
        assert {reason.value for reason in MissReason} == {
            "not_found",
            "model_mismatch",
            "quant_mismatch",
            "restore_failed",
            "restore_timeout",
            "prefix_mismatch",
        }


class TestTelemetryEvent:
    def test_missing_values_are_null_not_zero(self):
        """T-12 验收：缺失用 null，避免「0ms 是没有还是很快」的歧义。"""
        payload = TelemetryEvent(event="restore_miss", key="k", miss_reason="not_found").to_dict()
        assert payload["restore_ms"] is None
        assert payload["cold_prefill_ms"] is None
        assert payload["saved_bytes"] is None

    def test_source_distinguishes_local_kv_from_f15_cloud_cache(self):
        """T-12 要求显式标注「同名不同物」：我们的是本地 KV，不是云端前缀缓存。"""
        assert TelemetryEvent(event="restore_hit").source == "local_kv"

    def test_event_name_and_ts_are_populated(self):
        payload = TelemetryEvent(event="restore_hit", restore_ms=87.5).to_dict()
        assert payload["event"] == "restore_hit"
        assert payload["restore_ms"] == 87.5
        assert payload["ts"]
