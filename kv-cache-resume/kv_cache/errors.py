"""kv-cache-resume 策略层错误类型（T-05）。

设计原则：**降级逻辑按异常类型分支，不做字符串匹配**（T-09 的 `lookup` 全靠这里）。
每个类型都写清「什么情况下抛」，并标注它对应的 SPEC 需求编号。

SPEC 依据：REQ-E3（501 显式报错）、REQ-W1（失败回退冷 prefill）、REQ-W3（落盘失败只告警）、
REQ-W2（前缀不一致宁可重算）。
"""

from __future__ import annotations


class KVCacheError(Exception):
    """本包所有错误的基类。

    用途：让调用方可以 `except KVCacheError` 一次性兜住「缓存层的任何问题」，
    保证缓存故障永远不会穿透到 LexAgent 主链路（SPEC §2.2 非目标：不改主链路）。
    """


class KVCacheConfigError(KVCacheError, ValueError):
    """`KV_*` 配置非法时抛（T-05 追加）。

    抛点：`KVCacheConfig.from_env()` / `KVCacheConfig.__post_init__()`。
    语义：**非法值必须显式报错，不许静默取默认值** —— 一个写错的 `KV_MAX_BYTES`
    如果被默默当成默认值，会让 AC6（目录占用 ≤ 上限）在真正跑崩磁盘前看起来一直通过。
    """


class SlotApiUnavailable(KVCacheError):
    """llama-server 没有 slot 落盘能力 —— 即 save/restore 返回 **HTTP 501**。

    抛点：`engine.LlamaSlotClient` 收到 501 时（REQ-E3 的报错侧）。

    根因只有一个：**服务端启动时没有配 `--slot-save-path`**。故消息里必须点名这个 flag，
    让人一眼知道该改什么，而不是对着一个裸 `501` 猜。

    处置（REQ-E3 的降级侧，见 T-09）：整个缓存层进入 `UNCACHED` 模式，
    **后续不再发起任何 restore 调用**，但请求照常以冷 prefill 完成。
    """


class SlotApiError(KVCacheError):
    """除 501 以外的非 2xx 响应，或请求超时。

    抛点：`engine.LlamaSlotClient`（T-06）。

    属性：
        status_code: HTTP 状态码；超时等无响应场景为 `None`。
        body: **原始响应体**（截断到合理长度）—— 不丢现场，定位全靠它。
        timeout: 是否由超时导致。超时是独立语义：服务端可能只是慢，不是坏。
        url: 出错的请求地址。

    处置（REQ-W1 / AC5）：`lookup` 捕获后回退冷 prefill 并返回 MISS，
    **绝不中断请求**。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        timeout: bool = False,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.timeout = timeout
        self.url = url


class RestoreFailed(KVCacheError):
    """restore 动作本身失败，但**不是** HTTP 层错误。

    抛点：`policy.KVCachePolicy.lookup()`（T-09）。

    与 `SlotApiError` 的分工：`SlotApiError` 是「HTTP 没通」，本类型是「HTTP 通了，
    但这次恢复没成立」—— 例如服务端回 200 却没把 KV 装进目标 slot、
    或 restore 后的 token 序列校验对不上（REQ-W2）。

    处置（REQ-W1）：与 `SlotApiError` 同等对待 —— 回退冷 prefill，MISS 带 `restore_failed`。
    """


class SaveFailed(KVCacheError):
    """落盘链路任一环节失败：slot save / 写 meta / 更新 index。

    抛点：`policy.KVCachePolicy.persist()`（T-10）。

    处置（REQ-W3）：**只告警，不向上抛**。落盘是生成结束后的附加动作，
    失败了顶多下次少一次命中，绝不能影响已经生成好的答案。
    """


class PrefixMismatch(KVCacheError):
    """前缀 token 序列校验不一致（REQ-W2）。

    抛点：`prefix.canonicalize()` 检测到不稳定字段（时间戳 / 随机 ID）而拒绝缓存时；
    以及 `policy.lookup()` 在 restore 后核对 token 序列发现对不上时（T-09）。

    语义是**宁可重算，也不用错的 KV** —— 用错了的 KV 会产出看起来正常但内容错误的答案，
    这比慢一点严重得多。
    """


class IndexCorrupted(KVCacheError):
    """`index.json` 存在但无法解析（T-08 追加）。

    抛点：`store.CacheIndex.load()`。

    **为什么不偷偷当成空索引**：`index.json` 是最后的真相源。把它读成「什么都没有」，
    等于把所有 KV 文件一次性变成孤儿，接着启动扫描就会把它们全删掉 ——
    一次解析失败升级成一次删库。所以这里宁可显式报错、把坏文件挪到一边留现场，
    再由 `scan_and_repair()` 从各条目的 `meta.json` **重建**索引。
    """


__all__ = [
    "IndexCorrupted",
    "KVCacheError",
    "KVCacheConfigError",
    "PrefixMismatch",
    "RestoreFailed",
    "SaveFailed",
    "SlotApiError",
    "SlotApiUnavailable",
]
