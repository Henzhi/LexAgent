"""kv-cache-resume 策略层配置（T-05）。

全部 `KV_*` 环境变量集中在这里，**每个都有默认值**；非法值抛 `KVCacheConfigError`，
不静默取默认（T-05 验收清单第 2 条）。

命名清单与 `tickets/T-05-skeleton-and-config.md` 一一对应。唯一追加项是
`KV_CAPABILITY_TTL_S`（T-06 的能力探测缓存窗口，在 engine.py 里需要，已在
docs/phase1-design.md 备案）。

默认值的口径纪律（T-05 备注）：`KV_MAX_BYTES` / `KV_MAX_ENTRIES` / `KV_MIN_FREE_BYTES`
三个是**保守占位值，待 T-04 的实测 bytes/token 回填**，改的时候要连注释一起改。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from os import environ
from pathlib import Path
from urllib.parse import urlparse

from .errors import KVCacheConfigError

# --------------------------------------------------------------------------------------
# 默认值（env 名 → 默认值字符串）
# --------------------------------------------------------------------------------------

KV_QUANT_CHOICES: tuple[str, ...] = ("f16", "q8", "q4")

#: 相对路径的解析基准 —— `kv_cache/` 的上一级，即 `kv-cache-resume/` 目录。
#: 这样无论从仓库根还是子目录启动，`KV_CACHE_DIR=kv` 都落在同一个地方。
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

DEFAULT_ENV: dict[str, str] = {
    "KV_CACHE_DIR": "kv",
    "KV_ENABLED": "true",
    # 保守占位：3B 模型单 token KV 远小于 7B，先按 2 GiB 兜住；待 T-04 校准。
    "KV_MAX_BYTES": "2GiB",
    "KV_MAX_ENTRIES": "64",
    # 0 = 关闭水位保护（显式语义，不是静默绕过）
    "KV_MIN_FREE_BYTES": "1GiB",
    "KV_QUANT": "f16",
    "VERIFY_EXACT": "false",
    "RESTORE_TIMEOUT_S": "10",
    "SERVER_BASE_URL": "http://127.0.0.1:8080",
    # T-06 追加：能力探测结论的缓存窗口（秒），0 = 每次都重探
    "KV_CAPABILITY_TTL_S": "300",
}

_BOOL_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off", "n", "f"})

_BYTE_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}

_BYTE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]*)$")


# --------------------------------------------------------------------------------------
# 解析器：每个都带「变量名 + 原始值 + 期望形态」的报错
# --------------------------------------------------------------------------------------


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise KVCacheConfigError(f"{name}={raw!r} 不是合法布尔值；期望 true/false、1/0、yes/no、on/off（大小写不敏感）")


def _parse_bytes(name: str, raw: str) -> int:
    text = raw.strip().lower()
    match = _BYTE_RE.match(text)
    if match is None:
        raise KVCacheConfigError(f"{name}={raw!r} 不是合法字节数；期望整数或带单位，例如 512、'512MB'、'2GiB'")
    value, unit = match.group(1), match.group(2)
    if unit not in _BYTE_UNITS:
        raise KVCacheConfigError(
            f"{name}={raw!r} 的单位 {unit!r} 无法识别；支持 " + "/".join(sorted(u for u in _BYTE_UNITS if u))
        )
    return int(float(value) * _BYTE_UNITS[unit])


def _parse_int(name: str, raw: str) -> int:
    text = raw.strip()
    try:
        return int(text)
    except ValueError as exc:
        raise KVCacheConfigError(f"{name}={raw!r} 不是合法整数") from exc


def _parse_float(name: str, raw: str) -> float:
    text = raw.strip()
    try:
        return float(text)
    except ValueError as exc:
        raise KVCacheConfigError(f"{name}={raw!r} 不是合法数值（秒，允许小数）") from exc


def _parse_choice(name: str, raw: str, choices: tuple[str, ...]) -> str:
    lowered = raw.strip().lower()
    if lowered not in choices:
        raise KVCacheConfigError(f"{name}={raw!r} 不在允许取值 {list(choices)} 内")
    return lowered


def _parse_base_url(name: str, raw: str) -> str:
    text = raw.strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise KVCacheConfigError(
            f"{name}={raw!r} 不是合法的服务地址；期望形如 http://127.0.0.1:8080（带 scheme 与 host）"
        )
    return text


def _parse_dir(name: str, raw: str) -> Path:
    text = raw.strip()
    if not text:
        raise KVCacheConfigError(f"{name} 不能为空字符串")
    path = Path(text)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


# --------------------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KVCacheConfig:
    """策略层的全部运行参数（不可变）。

    构造方式优先用 `KVCacheConfig.from_env()`；直接 `KVCacheConfig(...)` 也会走
    `__post_init__` 校验，避免测试里手搓出非法组合。
    """

    cache_dir: Path
    enabled: bool = True
    max_bytes: int = 2 * 1024**3
    max_entries: int = 64
    min_free_bytes: int = 1024**3
    quant: str = "f16"
    verify_exact: bool = False
    restore_timeout_s: float = 10.0
    server_base_url: str = "http://127.0.0.1:8080"
    capability_ttl_s: float = 300.0

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise KVCacheConfigError(f"KV_MAX_BYTES 必须为正数，当前 {self.max_bytes}")
        if self.max_entries <= 0:
            raise KVCacheConfigError(f"KV_MAX_ENTRIES 必须为正数，当前 {self.max_entries}")
        if self.min_free_bytes < 0:
            raise KVCacheConfigError(f"KV_MIN_FREE_BYTES 不得为负，当前 {self.min_free_bytes}")
        if self.quant not in KV_QUANT_CHOICES:
            raise KVCacheConfigError(f"KV_QUANT={self.quant!r} 不在 {list(KV_QUANT_CHOICES)} 内")
        if self.restore_timeout_s <= 0:
            raise KVCacheConfigError(f"RESTORE_TIMEOUT_S 必须为正数，当前 {self.restore_timeout_s}")
        if self.capability_ttl_s < 0:
            raise KVCacheConfigError(f"KV_CAPABILITY_TTL_S 不得为负，当前 {self.capability_ttl_s}")
        if not str(self.cache_dir):
            raise KVCacheConfigError("KV_CACHE_DIR 不能为空")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KVCacheConfig:
        """从环境变量构造。`env` 可注入（默认读 `os.environ`），便于测试不污染进程环境。"""
        source: Mapping[str, str] = environ if env is None else env

        def raw(key: str) -> str:
            value = source.get(key)
            return DEFAULT_ENV[key] if value is None else value

        return cls(
            cache_dir=_parse_dir("KV_CACHE_DIR", raw("KV_CACHE_DIR")),
            enabled=_parse_bool("KV_ENABLED", raw("KV_ENABLED")),
            max_bytes=_parse_bytes("KV_MAX_BYTES", raw("KV_MAX_BYTES")),
            max_entries=_parse_int("KV_MAX_ENTRIES", raw("KV_MAX_ENTRIES")),
            min_free_bytes=_parse_bytes("KV_MIN_FREE_BYTES", raw("KV_MIN_FREE_BYTES")),
            quant=_parse_choice("KV_QUANT", raw("KV_QUANT"), KV_QUANT_CHOICES),
            verify_exact=_parse_bool("VERIFY_EXACT", raw("VERIFY_EXACT")),
            restore_timeout_s=_parse_float("RESTORE_TIMEOUT_S", raw("RESTORE_TIMEOUT_S")),
            server_base_url=_parse_base_url("SERVER_BASE_URL", raw("SERVER_BASE_URL")),
            capability_ttl_s=_parse_float("KV_CAPABILITY_TTL_S", raw("KV_CAPABILITY_TTL_S")),
        )

    @classmethod
    def env_names(cls) -> tuple[str, ...]:
        """本配置对象覆盖的全部环境变量名（供文档与测试反查，防止漏项）。"""
        return tuple(DEFAULT_ENV)

    def as_env(self) -> dict[str, str]:
        """回写成 env 字典（遥测 / 报告里记录生效配置用）。"""
        return {
            "KV_CACHE_DIR": str(self.cache_dir),
            "KV_ENABLED": str(self.enabled).lower(),
            "KV_MAX_BYTES": str(self.max_bytes),
            "KV_MAX_ENTRIES": str(self.max_entries),
            "KV_MIN_FREE_BYTES": str(self.min_free_bytes),
            "KV_QUANT": self.quant,
            "VERIFY_EXACT": str(self.verify_exact).lower(),
            "RESTORE_TIMEOUT_S": str(self.restore_timeout_s),
            "SERVER_BASE_URL": self.server_base_url,
            "KV_CAPABILITY_TTL_S": str(self.capability_ttl_s),
        }


def load_config(env: Mapping[str, str] | None = None) -> KVCacheConfig:
    """便捷入口：`load_config()` == `KVCacheConfig.from_env()`。

    刻意不做进程级缓存 —— 配置在测试与实验里频繁改写，缓存会让「改了 env 却没生效」
    变成一类难查的问题。
    """
    return KVCacheConfig.from_env(env)


def config_field_names() -> tuple[str, ...]:
    """`KVCacheConfig` 的字段名（供 design 文档与测试对照 env 清单）。"""
    return tuple(f.name for f in fields(KVCacheConfig))


__all__ = [
    "DEFAULT_ENV",
    "KV_QUANT_CHOICES",
    "PROJECT_ROOT",
    "KVCacheConfig",
    "config_field_names",
    "load_config",
]
