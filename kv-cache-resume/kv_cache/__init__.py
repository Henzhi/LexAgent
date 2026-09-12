"""kv-cache-resume · KV 缓存策略层。

把 llama.cpp 的 slot save/restore 包成一套**策略**：何时 save / 用什么 key / 何时 restore /
何时淘汰。llama.cpp 官方把「自动落盘」关闭为 not planned（feature request #17107），
理由就是「server 只提供机制，policy 留给 client」—— 所以本包才是这个 spike 的实质交付物。

用法草图（T-09 起可用）::

    from kv_cache import KVCacheConfig, KVCachePolicy

    policy = KVCachePolicy(KVCacheConfig.from_env())
    decision = policy.lookup(token_ids, model_id="qwen2.5-3b-instruct", quant="Q4_K_M")
    if decision.should_cold_prefill:
        ...  # 正常推理
    policy.persist(token_ids, "qwen2.5-3b-instruct", "Q4_K_M")

设计边界：**本包不 import 也不修改 LexAgent 的 `src/`**（SPEC §2.2 非目标）。
"""

from __future__ import annotations

from .config import DEFAULT_ENV, KV_QUANT_CHOICES, PROJECT_ROOT, KVCacheConfig, load_config
from .errors import (
    IndexCorrupted,
    KVCacheConfigError,
    KVCacheError,
    PrefixMismatch,
    RestoreFailed,
    SaveFailed,
    SlotApiError,
    SlotApiUnavailable,
)
from .models import (
    CacheMeta,
    Decision,
    DecisionKind,
    MissReason,
    TelemetryEvent,
    TelemetryEventName,
    now_iso,
)
from .policy import KVCachePolicy
from .prefix import KEY_HEX_LEN, PrefixKey, canonicalize, compute_key, detect_unstable, prefix_key_for_messages
from .store import CacheIndex, IndexEntry, IndexStats, RepairReport

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_ENV",
    "KEY_HEX_LEN",
    "KV_QUANT_CHOICES",
    "PROJECT_ROOT",
    "CacheIndex",
    "CacheMeta",
    "Decision",
    "DecisionKind",
    "IndexCorrupted",
    "IndexEntry",
    "IndexStats",
    "KVCacheConfig",
    "KVCacheConfigError",
    "KVCacheError",
    "KVCachePolicy",
    "MissReason",
    "PrefixKey",
    "PrefixMismatch",
    "RepairReport",
    "RestoreFailed",
    "SaveFailed",
    "SlotApiError",
    "SlotApiUnavailable",
    "TelemetryEvent",
    "TelemetryEventName",
    "__version__",
    "canonicalize",
    "compute_key",
    "detect_unstable",
    "load_config",
    "now_iso",
    "prefix_key_for_messages",
]
