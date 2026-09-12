"""跨模块数据结构契约（T-05）。

这里只放**跨模块共享**的数据定义：决策结果、元数据、遥测事件。
把它们集中在 models.py 而不是各自模块里，是为了避免 T-09 / T-10 / T-11 / T-12 之间
出现循环 import，也避免同名字段在不同模块里长出不同语义。

SPEC 依据：§4.3（元数据 schema）、REQ-E2 / REQ-E4（决策）、REQ-O2（遥测字段）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


def now_iso() -> str:
    """当前时间，带时区的 ISO 8601（`2026-09-11T21:00:00+08:00`）。

    刻意用 `astimezone()` 带上本地时区偏移 —— 裸 `datetime.now().isoformat()` 会丢时区，
    跨时区/夏令时排查实验数据时是个隐形坑（T-08 验收清单也点名了这条）。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ensure_aware(field_name: str, value: str) -> str:
    """校验时间戳带时区，不带就报错（而不是补齐成 UTC 蒙混过关）。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name}={value!r} 不是合法 ISO 8601 时间戳") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name}={value!r} 缺少时区偏移（期望形如 2026-09-11T21:00:00+08:00）")
    return value


class DecisionKind(str, Enum):
    """`policy.lookup()` 的三种出口（T-05 状态机的三个终点）。"""

    HIT = "hit"
    """键命中 + 元数据校验通过 + restore 成功。可以直接接着 decode。"""

    MISS = "miss"
    """未命中，或命中但校验/恢复失败。调用方应走冷 prefill。**不中断请求**（REQ-W1）。"""

    UNCACHED = "uncached"
    """服务端没有 slot 落盘能力（501）。不发起任何 restore 调用，直接冷 prefill（REQ-E3）。"""


class MissReason(str, Enum):
    """MISS 的原因枚举 —— T-09 验收要求「reason 是枚举」，用于告警与遥测归因。

    刻意区分 `MODEL_MISMATCH` 与 `QUANT_MISMATCH`：一个是换了模型，一个是换了 KV 精度，
    排查时的动作完全不同（前者要重建索引，后者只需确认 `KV_QUANT` 配置）。
    """

    NOT_FOUND = "not_found"
    MODEL_MISMATCH = "model_mismatch"
    QUANT_MISMATCH = "quant_mismatch"
    RESTORE_FAILED = "restore_failed"
    RESTORE_TIMEOUT = "restore_timeout"
    PREFIX_MISMATCH = "prefix_mismatch"


@dataclass(frozen=True)
class CacheMeta:
    """单个 KV 缓存条目的元数据，字段**逐字段**对齐 SPEC §4.3。

    存储在 `<KV_CACHE_DIR>/slotcache_{key}.meta.json`。
    """

    key: str
    model_id: str
    quant: str
    n_tokens: int
    bytes: int
    created_at: str
    last_used_at: str
    hits: int = 0

    def __post_init__(self) -> None:
        _ensure_aware("created_at", self.created_at)
        _ensure_aware("last_used_at", self.last_used_at)
        if self.n_tokens < 0 or self.bytes < 0 or self.hits < 0:
            raise ValueError("CacheMeta 的 n_tokens / bytes / hits 不得为负")

    def to_dict(self) -> dict[str, Any]:
        """转成 dict，字段顺序与 SPEC §4.3 示例一致（人读 JSON 时更顺）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheMeta:
        """从 dict 还原，**严格校验字段完整性**（缺字段直接报错，不补默认值）。

        严格是刻意的：索引文件是「最后的真相源」（T-08 备注），
        半截或旧版 schema 的 meta 被宽容地读进来，只会把损坏状态往后传。
        """
        required = ("key", "model_id", "quant", "n_tokens", "bytes", "created_at", "last_used_at")
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError(f"CacheMeta 缺少必填字段：{missing}")
        unknown = [name for name in data if name not in {*required, "hits"}]
        if unknown:
            raise ValueError(f"CacheMeta 含未知字段：{unknown}")
        return cls(
            key=str(data["key"]),
            model_id=str(data["model_id"]),
            quant=str(data["quant"]),
            n_tokens=int(data["n_tokens"]),
            bytes=int(data["bytes"]),
            created_at=str(data["created_at"]),
            last_used_at=str(data["last_used_at"]),
            hits=int(data.get("hits", 0)),
        )

    def touched(self, *, at: str | None = None) -> CacheMeta:
        """命中后返回新实例：`hits + 1`、`last_used_at` 前进（REQ-E4，T-09 用）。"""
        return CacheMeta(
            key=self.key,
            model_id=self.model_id,
            quant=self.quant,
            n_tokens=self.n_tokens,
            bytes=self.bytes,
            created_at=self.created_at,
            last_used_at=at or now_iso(),
            hits=self.hits + 1,
        )


@dataclass(frozen=True)
class Decision:
    """`policy.lookup()` 的返回值 —— 一个入口三种形态，非法组合在构造时就拦住。

    用工厂方法构造（`Decision.hit` / `Decision.miss` / `Decision.uncached`），
    别手工拼字段：`__post_init__` 会校验组合合法性。
    """

    kind: DecisionKind
    key: str | None = None
    filename: str | None = None
    reason: MissReason | None = None
    meta: CacheMeta | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind is DecisionKind.HIT:
            if not self.key or not self.filename:
                raise ValueError("HIT 必须带 key 与 filename")
            if self.reason is not None:
                raise ValueError("HIT 不应带 miss reason")
        elif self.kind is DecisionKind.MISS:
            if self.reason is None:
                raise ValueError("MISS 必须带 reason ——「失败不抛」不等于「失败静默」（T-09 备注）")
            if self.filename is not None:
                raise ValueError("MISS 不应带 filename")
        elif self.kind is DecisionKind.UNCACHED and self.filename is not None:
            raise ValueError("UNCACHED 不应带 filename")

    @property
    def hit(self) -> bool:
        return self.kind is DecisionKind.HIT

    @property
    def should_cold_prefill(self) -> bool:
        """是否需要走冷 prefill —— MISS 与 UNCACHED 都是。"""
        return self.kind is not DecisionKind.HIT

    @classmethod
    def hit_(cls, *, key: str, filename: str, meta: CacheMeta | None = None, detail: str | None = None) -> Decision:
        return cls(kind=DecisionKind.HIT, key=key, filename=filename, meta=meta, detail=detail)

    @classmethod
    def miss(cls, reason: MissReason, *, key: str | None = None, detail: str | None = None) -> Decision:
        return cls(kind=DecisionKind.MISS, key=key, reason=reason, detail=detail)

    @classmethod
    def uncached(cls, *, key: str | None = None, detail: str | None = None) -> Decision:
        return cls(kind=DecisionKind.UNCACHED, key=key, detail=detail)


class TelemetryEventName(str, Enum):
    """遥测事件名（REQ-O2 逐字要求前五个）。"""

    RESTORE_HIT = "restore_hit"
    RESTORE_MISS = "restore_miss"
    RESTORE_UNCACHED = "restore_uncached"
    SAVED_BYTES = "saved_bytes"
    RESTORE_MS = "restore_ms"
    COLD_PREFILL_MS = "cold_prefill_ms"


@dataclass(frozen=True)
class TelemetryEvent:
    """一次请求一行可机读遥测（T-12 输出，这里先定字段）。

    **缺失值统一用 `None`（JSON `null`），不用 0** —— T-12 验收点名了这个歧义：
    `restore_ms = 0` 到底是「没有恢复」还是「恢复快到测不出来」，无法区分。
    """

    event: str
    ts: str = field(default_factory=now_iso)
    key: str | None = None
    model_id: str | None = None
    quant: str | None = None
    n_tokens: int | None = None
    hit: bool | None = None
    miss_reason: str | None = None
    saved_bytes: int | None = None
    restore_ms: float | None = None
    cold_prefill_ms: float | None = None
    #: 来源标识：与 LexAgent F15 的「云端前缀缓存」同名不同物，必须显式区分（T-12）
    source: str = "local_kv"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "CacheMeta",
    "Decision",
    "DecisionKind",
    "MissReason",
    "TelemetryEvent",
    "TelemetryEventName",
    "now_iso",
]
