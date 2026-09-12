"""策略层门面 `KVCachePolicy`（T-05 只定契约，实现分属后续票）。

⚠️ 本文件在 T-05 阶段**只有构造函数与签名**，三个方法一律 `raise NotImplementedError`。
落地分工：

| 方法 | 由谁实现 | 依据 |
| :--- | :--- | :--- |
| `lookup()` | T-09 | REQ-E2 / E4 / U2 / W1 / W2、AC5 |
| `persist()` | T-10 | REQ-E1 / W3 |
| `enforce_limits()` | T-11 | REQ-S1 / S2 / W4、AC6 |

构造函数在这里就把 T-09 与 T-10 的**共享状态**（索引句柄、per-key 锁）初始化好 ——
T-09 备注明确要求这一点，避免 T-10 回过头来改构造函数签名。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from .config import KVCacheConfig
from .models import Decision


class KVCachePolicy:
    """KV 缓存的决策中枢：何时 save / 用什么 key / 何时 restore / 何时淘汰。

    纯粹的选择逻辑放在这里；真正的 IO 在 `engine.LlamaSlotClient`（HTTP）
    与 `store.CacheIndex`（磁盘）里。**这一层不直接发 HTTP、不直接开文件。**

    构造参数全部注入（`config` / `client` / `store`），便于离线单测替换为 fake ——
    AC5 的「强制失败」要求把 restore 钉死为失败，只有注入才能做到每一次都覆盖。
    """

    def __init__(
        self,
        config: KVCacheConfig,
        client: Any | None = None,
        store: Any | None = None,
    ) -> None:
        self.config = config
        #: T-06 的 `LlamaSlotClient`；T-05 阶段允许为 None
        self.client = client
        #: T-08 的 `CacheIndex`；T-05 阶段允许为 None
        self.store = store
        # T-09 / T-10 共享的 per-key 串行锁（REQ-U3：同 key 单写者）
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        """取该 key 的进程内锁；同一 key 恒定拿到同一把（REQ-U3）。

        边界（T-08 备注）：**仅进程内有效，不跨进程**。spike 定位如此，
        将来要跨进程得换成文件锁，别以为这里是万能的。
        """
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def lookup(self, token_ids: Sequence[int], model_id: str, quant: str) -> Decision:
        """判断这次请求能否复用已有 KV，需要时执行 restore。

        返回 `Decision`（HIT / MISS / UNCACHED），**任何失败都不得抛出**（REQ-W1 / AC5）。
        """
        raise NotImplementedError("T-09 实现：命中判定与 restore 全流程（REQ-E2/E4/W1/W2、AC5）")

    def persist(
        self,
        token_ids: Sequence[int],
        model_id: str,
        quant: str,
        *,
        completed: bool = True,
        filename: str | None = None,
    ) -> bool:
        """生成结束后把当前 slot 的 KV 落盘并更新索引。

        `completed=False`（异常中断 / 用户取消）时**不落盘**（REQ-E1 排除项）。
        失败只告警，返回 False 表示本次未落盘，不抛异常（REQ-W3）。
        """
        raise NotImplementedError("T-10 实现：落盘触发与 save 语义（REQ-E1/W3）")

    def enforce_limits(self) -> None:
        """执行容量上限与磁盘水位检查（在 `persist` 之前调用）。

        T-11 实现：LRU 淘汰 + 水位状态机（REQ-S1/S2/W4、AC6）。
        """
        raise NotImplementedError("T-11 实现：LRU 淘汰与磁盘水位保护（REQ-S1/S2/W4、AC6）")


__all__ = ["KVCachePolicy"]
