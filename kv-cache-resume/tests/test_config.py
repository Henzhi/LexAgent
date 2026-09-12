"""T-05 验收：`KV_*` 环境变量覆盖 + **非法值必须显式报错**。

对应验收清单第 2 条：「`KV_*` 环境变量覆盖有单测，且非法值报错清晰（不静默吞掉）」。
"""

from __future__ import annotations

import pytest

from kv_cache import KVCacheConfig, KVCacheConfigError
from kv_cache.config import DEFAULT_ENV, KV_QUANT_CHOICES, PROJECT_ROOT, load_config


class TestDefaults:
    def test_empty_env_falls_back_to_defaults(self, env):
        cfg = KVCacheConfig.from_env(env)
        assert cfg.enabled is True
        assert cfg.quant == "f16"
        assert cfg.verify_exact is False
        assert cfg.max_entries == 64
        assert cfg.max_bytes == 2 * 1024**3
        assert cfg.min_free_bytes == 1024**3
        assert cfg.restore_timeout_s == 10.0
        assert cfg.server_base_url == "http://127.0.0.1:8080"
        assert cfg.capability_ttl_s == 300.0

    def test_load_config_is_thin_wrapper(self, env):
        assert load_config(env) == KVCacheConfig.from_env(env)

    def test_default_cache_dir_is_relative_to_project_root(self, env):
        """`KV_CACHE_DIR=kv` 必须落在 `kv-cache-resume/kv`，而不是「当前工作目录」。"""
        cfg = KVCacheConfig.from_env(env)
        assert cfg.cache_dir.is_absolute()
        assert cfg.cache_dir == PROJECT_ROOT / "kv"

    def test_absolute_cache_dir_is_kept_as_is(self, tmp_path):
        cfg = KVCacheConfig.from_env({"KV_CACHE_DIR": str(tmp_path)})
        assert cfg.cache_dir == tmp_path


class TestOverrides:
    @pytest.mark.parametrize(
        ("key", "raw", "attr", "expected"),
        [
            ("KV_ENABLED", "false", "enabled", False),
            ("KV_ENABLED", "0", "enabled", False),
            ("KV_ENABLED", "No", "enabled", False),
            ("KV_MAX_BYTES", "512MB", "max_bytes", 512 * 1000**2),
            ("KV_MAX_BYTES", "3GiB", "max_bytes", 3 * 1024**3),
            ("KV_MAX_BYTES", "1024", "max_bytes", 1024),
            ("KV_MAX_ENTRIES", "8", "max_entries", 8),
            ("KV_MIN_FREE_BYTES", "0", "min_free_bytes", 0),
            ("KV_QUANT", "Q8", "quant", "q8"),
            ("KV_QUANT", "q4", "quant", "q4"),
            ("VERIFY_EXACT", "true", "verify_exact", True),
            ("RESTORE_TIMEOUT_S", "0.5", "restore_timeout_s", 0.5),
            ("KV_CAPABILITY_TTL_S", "0", "capability_ttl_s", 0.0),
        ],
    )
    def test_single_override(self, env, key, raw, attr, expected):
        env[key] = raw
        assert getattr(KVCacheConfig.from_env(env), attr) == expected

    def test_server_base_url_trailing_slash_is_normalized(self, env):
        env["SERVER_BASE_URL"] = "http://127.0.0.1:8080/"
        assert KVCacheConfig.from_env(env).server_base_url == "http://127.0.0.1:8080"

    def test_env_names_cover_every_default(self):
        """env 清单与 DEFAULT_ENV 不许脱钩 —— 加了配置项却忘了默认值会在这里炸。"""
        assert KVCacheConfig.env_names() == tuple(DEFAULT_ENV)

    def test_as_env_roundtrip(self, env):
        """`as_env()` 回写后能原样还原（遥测/报告里记录生效配置就靠它）。"""
        cfg = KVCacheConfig.from_env({**env, "KV_QUANT": "q8", "KV_MAX_BYTES": "1GiB"})
        assert KVCacheConfig.from_env(cfg.as_env()) == cfg


class TestIllegalValues:
    """非法值必须**指名报错**，不许静默取默认。"""

    def test_illegal_bool(self, env):
        env["KV_ENABLED"] = "maybe"
        with pytest.raises(KVCacheConfigError) as excinfo:
            KVCacheConfig.from_env(env)
        message = str(excinfo.value)
        assert "KV_ENABLED" in message and "maybe" in message

    def test_illegal_int(self, env):
        env["KV_MAX_ENTRIES"] = "many"
        with pytest.raises(KVCacheConfigError, match="KV_MAX_ENTRIES"):
            KVCacheConfig.from_env(env)

    def test_illegal_float(self, env):
        env["RESTORE_TIMEOUT_S"] = "fast"
        with pytest.raises(KVCacheConfigError, match="RESTORE_TIMEOUT_S"):
            KVCacheConfig.from_env(env)

    def test_illegal_bytes_unit(self, env):
        env["KV_MAX_BYTES"] = "2PB"
        with pytest.raises(KVCacheConfigError, match="KV_MAX_BYTES"):
            KVCacheConfig.from_env(env)

    def test_illegal_bytes_text(self, env):
        env["KV_MAX_BYTES"] = "big"
        with pytest.raises(KVCacheConfigError, match="KV_MAX_BYTES"):
            KVCacheConfig.from_env(env)

    def test_illegal_quant(self, env):
        env["KV_QUANT"] = "q2"
        with pytest.raises(KVCacheConfigError) as excinfo:
            KVCacheConfig.from_env(env)
        assert "KV_QUANT" in str(excinfo.value)
        assert all(choice in str(excinfo.value) for choice in KV_QUANT_CHOICES)

    @pytest.mark.parametrize("raw", ["127.0.0.1:8080", "ftp://host", "http://", ""])
    def test_illegal_base_url(self, env, raw):
        env["SERVER_BASE_URL"] = raw
        with pytest.raises(KVCacheConfigError, match="SERVER_BASE_URL"):
            KVCacheConfig.from_env(env)

    def test_empty_cache_dir(self, env):
        env["KV_CACHE_DIR"] = "   "
        with pytest.raises(KVCacheConfigError, match="KV_CACHE_DIR"):
            KVCacheConfig.from_env(env)

    def test_illegal_value_does_not_silently_fall_back(self, env):
        """反向守门：报错之后**不能**悄悄返回默认配置。"""
        env["KV_MAX_BYTES"] = "oops"
        with pytest.raises(KVCacheConfigError):
            KVCacheConfig.from_env(env)


class TestDirectConstruction:
    """直接构造也要校验 —— 否则测试里手搓的非法配置会绕过所有检查。"""

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"max_bytes": 0}, "KV_MAX_BYTES"),
            ({"max_entries": -1}, "KV_MAX_ENTRIES"),
            ({"min_free_bytes": -5}, "KV_MIN_FREE_BYTES"),
            ({"quant": "q2"}, "KV_QUANT"),
            ({"restore_timeout_s": 0}, "RESTORE_TIMEOUT_S"),
            ({"capability_ttl_s": -1}, "KV_CAPABILITY_TTL_S"),
        ],
    )
    def test_post_init_rejects(self, tmp_path, kwargs, field):
        with pytest.raises(KVCacheConfigError, match=field):
            KVCacheConfig(cache_dir=tmp_path / "kv", **kwargs)

    def test_config_is_frozen(self, config):
        with pytest.raises(Exception):
            config.quant = "q4"  # type: ignore[misc]
