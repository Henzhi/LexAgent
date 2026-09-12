"""T-05 验收：骨架可导入、契约签名就位、共享状态已初始化。

验收清单第 1 条：`python -c "from kv_cache import KVCachePolicy"` 成功。
第 3 条：每个错误类型有 docstring 说明与 REQ 的对应关系。
"""

from __future__ import annotations

import re

import pytest

import kv_cache
from kv_cache import (
    KVCacheConfigError,
    KVCacheError,
    KVCachePolicy,
    PrefixMismatch,
    RestoreFailed,
    SaveFailed,
    SlotApiError,
    SlotApiUnavailable,
)

ERROR_TYPES = (
    KVCacheConfigError,
    SlotApiUnavailable,
    SlotApiError,
    RestoreFailed,
    SaveFailed,
    PrefixMismatch,
)


class TestPackageImport:
    def test_public_api_is_importable(self):
        assert KVCachePolicy is not None
        assert kv_cache.__version__

    def test_version_is_declared(self):
        assert kv_cache.__version__.count(".") == 2


class TestPolicySkeleton:
    def test_constructible_with_config_only(self, config):
        """T-05 阶段 client / store 还不存在，允许只传 config。"""
        policy = KVCachePolicy(config)
        assert policy.config is config
        assert policy.client is None and policy.store is None

    def test_accepts_injected_client_and_store(self, config):
        client, store = object(), object()
        policy = KVCachePolicy(config, client=client, store=store)
        assert policy.client is client and policy.store is store

    @pytest.mark.parametrize("method_name", ["lookup", "persist", "enforce_limits"])
    def test_methods_are_deliberate_stubs(self, config, method_name):
        """三个方法在 T-05 阶段刻意留空 —— 报错信息必须指向承接它的那张票。"""
        policy = KVCachePolicy(config)
        with pytest.raises(NotImplementedError) as excinfo:
            if method_name == "lookup":
                policy.lookup([1, 2, 3], "m", "f16")
            elif method_name == "persist":
                policy.persist([1, 2, 3], "m", "f16")
            else:
                policy.enforce_limits()
        assert "T-1" in str(excinfo.value) or "T-0" in str(excinfo.value)


class TestSharedState:
    """T-09 备注要求：lookup 与 persist 的共享状态（per-key 锁）在 T-05 就初始化好。"""

    def test_same_key_gets_same_lock(self, config):
        policy = KVCachePolicy(config)
        assert policy._lock_for("k1") is policy._lock_for("k1")

    def test_different_keys_get_different_locks(self, config):
        policy = KVCachePolicy(config)
        assert policy._lock_for("k1") is not policy._lock_for("k2")


class TestErrorTaxonomy:
    def test_all_errors_share_a_base(self):
        """缓存层任何问题都能被一次 `except KVCacheError` 兜住（SRC 主链路不受影响）。"""
        for error_type in ERROR_TYPES:
            assert issubclass(error_type, KVCacheError)

    def test_config_error_is_also_value_error(self):
        """便于既有对 `ValueError` 的调用方自然接住配置错误。"""
        assert issubclass(KVCacheConfigError, ValueError)

    @pytest.mark.parametrize("error_type", ERROR_TYPES)
    def test_each_error_documents_raise_conditions_and_traceability(self, error_type):
        """T-05 验收第 3 条：每个错误都要写清「什么情况下抛」并留可追溯的锚点。

        锚点可以是 SPEC 的 `REQ-*`，也可以是本目录的票号 `T-xx` ——
        `KVCacheConfigError` 是 T-05 自己的产物，不对应某条 REQ，
        硬编一个需求编号反而是在编造出处。
        """
        doc = error_type.__doc__ or ""
        assert "抛点" in doc or "抛" in doc, f"{error_type.__name__} 缺少抛点说明"
        assert re.search(r"REQ-|T-\d", doc), f"{error_type.__name__} 未标注 SPEC 需求编号或票号"

    def test_slot_api_error_keeps_raw_response(self):
        """REQ-E3 / R6：定位全靠现场，原始响应体不许丢。"""
        error = SlotApiError("boom", status_code=500, body='{"error":"nope"}', url="http://x/slots/0")
        assert error.status_code == 500
        assert error.body == '{"error":"nope"}'
        assert error.timeout is False
        assert error.url == "http://x/slots/0"

    def test_slot_api_error_timeout_flag(self):
        assert SlotApiError("slow", timeout=True).timeout is True

    def test_slot_api_unavailable_points_at_root_cause(self):
        """REQ-E3 的实质要求：消息必须人可读且指向 `--slot-save-path`。"""
        assert "--slot-save-path" in (SlotApiUnavailable.__doc__ or "")
