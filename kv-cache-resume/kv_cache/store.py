"""索引与元数据存储（T-08）—— 本包唯一碰磁盘的地方。

文件布局严格对齐 SPEC §4.4::

    <KV_CACHE_DIR>/
    ├── slotcache_{key}.bin           # KV 张量本体（由引擎落盘，本层只认文件存不存在）
    ├── slotcache_{key}.meta.json     # 条目自身的权威记录（CacheMeta，逐字段对齐 §4.3）
    └── index.json                    # 全局索引：key → meta 路径 + 容量统计

--------------------------------------------------------------------------- 核心不变式

**`index.json` 是最后的真相源：它要么没有某个 key，要么指向真实存在的文件。**

写入顺序（T-10 用）：`bin` → `meta` → `index`。
删除顺序（T-11 用）：摘 `index` 条目 → 删文件。
两个方向中断都只会留下**孤儿文件**，绝不会出现「index 指向不存在的文件」。
孤儿由 `scan_and_repair()` 在启动时兜底回收。

- **`index.json` 是派生缓存，不是权威**：条目自身的真相在 `slotcache_{key}.meta.json`。
  index 只额外缓存容量与 LRU 需要的字段（`bytes` / `last_used_at`），
  这样 T-11 做淘汰决策不必把每个 meta 都读一遍。
- 元数据 schema 逐字段对齐 SPEC §4.3，时间戳带时区（见 `models.now_iso`）。

--------------------------------------------------------------------------- 锁边界（务必读）

**本层的锁只在进程内有效，跨进程不保证。**

- 同 key 串行：`_key_locks` 里的 per-key 锁（REQ-U3 的单写者纪律）。
- `index.json`：`_index_lock` 一把全局锁。
- **跨进程不做**（spike 定位，见 T-08「不做」）：两个进程同时指向同一个 `KV_CACHE_DIR`
  会互相覆盖索引。要跨进程得上文件锁（`msvcrt.locking` / `fcntl.flock`），本票刻意不做。
  启动扫描只能兜住「已经坏掉的孤儿」，**兜不住两个进程同时写的竞态**。

与 `policy._key_locks` 的分层：policy 那把是**决策级**（把 lookup/persist 的读-改-写整体圈住），
本层这把是**文件级**（保证单个文件的读改写不被穿插）。两层都从外往内获取，
不会反向嵌套，故无死锁。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import IndexCorrupted, SaveFailed
from .models import CacheMeta, now_iso
from .prefix import KEY_HEX_LEN

#: 索引文件格式版本。字段结构变化时递增，`load()` 会对不上版本直接报错而不是猜。
INDEX_VERSION = 1

BIN_SUFFIX = ".bin"
META_SUFFIX = ".meta.json"
INDEX_NAME = "index.json"
SLOTCACHE_PREFIX = "slotcache_"

#: key 只接受定长小写十六进制。**这不只是格式校验** —— key 会被拼进文件名，
#: 一个形如 `../x` 的 key 能直接穿越出缓存目录。`prefix.compute_key()` 天然满足这个形状，
#: 但存储层不该假设调用方一定是它。
_KEY_RE = re.compile(rf"^[0-9a-f]{{{KEY_HEX_LEN}}}$")


# --------------------------------------------------------------------------------------
# 返回值
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    """索引里的一条**轻量**记录（不含完整 meta，避免为淘汰决策读一堆文件）。

    `meta_file` 是纯文件名（不是路径）—— index 里存绝对路径会让整个目录不可搬迁。
    """

    key: str
    meta_file: str
    bytes: int
    last_used_at: str


@dataclass(frozen=True)
class IndexStats:
    """容量口径。**两个字节口径必须分开**，混用会让 AC6 对不上：

    - `bytes_claimed`：各条目 meta 自报的 `bytes` 之和。写盘时记的，不随外部改动变化。
    - `bytes_on_disk`：bin 文件**实际**占用的字节之和。AC6（目录占用 ≤ `KV_MAX_BYTES`）
      必须用这个 —— 用自报值等于拿「我以为写了多少」去比上限。
    """

    entries: int
    bytes_claimed: int
    bytes_on_disk: int


@dataclass(frozen=True)
class RepairReport:
    """启动扫描的结果。计数会被 T-12 的遥测读走（T-08 验收清单点名）。"""

    scanned_bins: int
    scanned_metas: int
    orphan_bins_removed: list[str]
    orphan_metas_removed: list[str]
    index_entries_repaired: int
    index_rebuilt: bool

    @property
    def total_cleaned(self) -> int:
        return len(self.orphan_bins_removed) + len(self.orphan_metas_removed)


# --------------------------------------------------------------------------------------
# 原子写
# --------------------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    """`tmp` + `os.replace` 写入，**任何时刻目标文件都是完整的**。

    硬要求，不是可选项：`json.dump` 直接写目标文件是最常见的「半截 JSON」来源 ——
    进程在写第 500 字节时被杀，文件就废了。而 `index.json` 废掉一次就是整库失联。

    实现要点：

    - tmp 名用 `mkstemp` 生成（含随机段）—— 两个线程同时写同一目标时，
      若共用固定 tmp 名，A 的 `os.replace` 可能把 B 正在写的 tmp 搬走。
    - 写完 `flush` + `fsync` 再 replace，避免「replace 成功但内容还在页缓存」时掉电丢数据。
    - 失败时**删掉 tmp**：否则目录里会攒下 `.index.json.xxx.tmp` 这种半截文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 目标文件保持原样（replace 还没发生），把 tmp 清掉不留垃圾
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# --------------------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------------------


class CacheIndex:
    """`KV_CACHE_DIR` 的索引层：读写 meta、维护 `index.json`、启动时修一致性。

    构造参数：
        cache_dir: 缓存目录（不存在会在首次写入时创建）。

    **锁边界**：per-key 锁与 `index.json` 的全局锁**都只在进程内有效，跨进程不保证**。
    两个进程指向同一个 `KV_CACHE_DIR` 会互相覆盖索引 —— 跨进程需要文件锁，本票刻意不做
    （见模块 docstring 的「锁边界」）。别把这里当线程安全的万能实现用。
    """

    def __init__(self, cache_dir: Path | str) -> None:
        self.cache_dir = Path(cache_dir)
        self._index_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    @classmethod
    def from_config(cls, config) -> CacheIndex:
        return cls(config.cache_dir)

    # ---------------------------------------------------------------- 路径

    @property
    def index_path(self) -> Path:
        return self.cache_dir / INDEX_NAME

    def bin_path(self, key: str) -> Path:
        self._require_valid_key(key)
        return self.cache_dir / f"{SLOTCACHE_PREFIX}{key}{BIN_SUFFIX}"

    def meta_path(self, key: str) -> Path:
        self._require_valid_key(key)
        return self.cache_dir / f"{SLOTCACHE_PREFIX}{key}{META_SUFFIX}"

    @staticmethod
    def _require_valid_key(key: str) -> None:
        if not _KEY_RE.match(key):
            raise ValueError(
                f"非法缓存 key {key!r}：期望 {KEY_HEX_LEN} 位小写十六进制。"
                "key 会被拼进文件名，不做校验就等于允许路径穿越（`../x` 能写出缓存目录）。"
            )

    # ---------------------------------------------------------------- 锁

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    # ---------------------------------------------------------------- 索引读写

    def load(self) -> dict[str, IndexEntry]:
        """读 `index.json`。

        - 文件不存在 → 空索引（首次运行，正常）。
        - 文件**存在但解析失败** → 抛 `IndexCorrupted`（**不**当成空索引，见该异常 docstring），
          并把坏文件改名为 `index.json.corrupt-<时间戳>` 留现场，避免下次又被绊一次。
        """
        if not self.index_path.exists():
            return {}
        raw = self.index_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"顶层不是对象而是 {type(data).__name__}")
            if data.get("version") != INDEX_VERSION:
                raise ValueError(f"版本号 {data.get('version')!r} 与当前 {INDEX_VERSION} 不符")
            entries = data["entries"]
            if not isinstance(entries, dict):
                raise ValueError("entries 不是对象")
            return {key: self._entry_from_dict(key, value) for key, value in entries.items()}
        except (ValueError, KeyError, TypeError) as exc:
            quarantined = self._quarantine_index()
            raise IndexCorrupted(
                f"{self.index_path} 无法解析（{exc}）。"
                f"已把坏文件移到 {quarantined}，现场保留。"
                "索引是最后的真相源，读成「空」会让所有 KV 文件变成孤儿后被清理，"
                "故此处显式报错；用 scan_and_repair() 从各条目 meta.json 重建即可。"
            ) from exc

    def _quarantine_index(self) -> Path:
        stamp = now_iso().replace(":", "").replace("+", "_")
        target = self.index_path.with_name(f"{INDEX_NAME}.corrupt-{stamp}")
        with contextlib.suppress(OSError):
            os.replace(self.index_path, target)
        return target

    @staticmethod
    def _entry_from_dict(key: str, value: object) -> IndexEntry:
        if not isinstance(value, dict):
            raise ValueError(f"entries[{key!r}] 不是对象")
        return IndexEntry(
            key=key,
            meta_file=str(value["meta"]),
            bytes=int(value["bytes"]),
            last_used_at=str(value["last_used_at"]),
        )

    def _dump(self, entries: dict[str, IndexEntry]) -> None:
        """原子写 `index.json`。调用方必须已持有 `_index_lock`。"""
        payload = {
            "version": INDEX_VERSION,
            "updated_at": now_iso(),
            "entries": {
                key: {"meta": entry.meta_file, "bytes": entry.bytes, "last_used_at": entry.last_used_at}
                for key, entry in sorted(entries.items())
            },
            "totals": {
                "entries": len(entries),
                "bytes": sum(entry.bytes for entry in entries.values()),
            },
        }
        atomic_write_text(self.index_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    # ---------------------------------------------------------------- 条目读写

    def _write_meta(self, meta: CacheMeta) -> None:
        atomic_write_text(self.meta_path(meta.key), json.dumps(meta.to_dict(), ensure_ascii=False, indent=2) + "\n")

    def read_meta(self, key: str) -> CacheMeta | None:
        """从 `slotcache_{key}.meta.json` 读条目权威记录。文件不存在返回 None。"""
        path = self.meta_path(key)
        if not path.exists():
            return None
        return CacheMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def get(self, key: str) -> CacheMeta | None:
        """取条目。**走 meta.json 而不是 index** —— index 只是派生缓存，不是权威。"""
        return self.read_meta(key)

    def register(self, meta: CacheMeta) -> IndexEntry:
        """登记一个刚落盘的条目：写 `meta` → 更新 `index`（T-10 的第三步）。

        ⚠️ 调用前 **bin 必须已经存在**（写入顺序：bin → meta → index）。
        这里会校验；不校验的话就可能写出「index 指向不存在的文件」，
        而那正是本层唯一要守住的不变式。
        """
        self._require_valid_key(meta.key)
        if not self.bin_path(meta.key).exists():
            raise SaveFailed(
                f"登记 {meta.key} 失败：{self.bin_path(meta.key)} 不存在。"
                "写入顺序必须是 bin → meta → index；先写 index 会造出「索引指向不存在的文件」，"
                "破坏本层的不变式。"
            )
        with self._lock_for(meta.key):
            self._write_meta(meta)
            with self._index_lock:
                entries = self.load()
                entries[meta.key] = IndexEntry(
                    key=meta.key,
                    meta_file=self.meta_path(meta.key).name,
                    bytes=meta.bytes,
                    last_used_at=meta.last_used_at,
                )
                self._dump(entries)
        return entries[meta.key]

    def touch(self, key: str, *, at: str | None = None) -> CacheMeta | None:
        """命中后更新 `hits` 与 `last_used_at`（REQ-E4，T-09 调用）。条目不存在返回 None。

        读 meta → 写 meta → 同步 index，全程持 per-key 锁，故并发 touch 不会丢计数。
        """
        with self._lock_for(key):
            meta = self.read_meta(key)
            if meta is None:
                return None
            touched = meta.touched(at=at)
            self._write_meta(touched)
            with self._index_lock:
                entries = self.load()
                if key in entries:
                    entries[key] = IndexEntry(
                        key=key,
                        meta_file=entries[key].meta_file,
                        bytes=touched.bytes,
                        last_used_at=touched.last_used_at,
                    )
                    self._dump(entries)
            return touched

    def drop_from_index(self, key: str) -> bool:
        """**删除流程的第一步**：只摘 `index` 条目，不碰任何文件（T-11 调用）。

        删除顺序与写入顺序方向一致（先摘索引再删文件），中断同样只留孤儿。
        返回是否真的摘掉了一条。
        """
        with self._lock_for(key):
            with self._index_lock:
                entries = self.load()
                if key not in entries:
                    return False
                del entries[key]
                self._dump(entries)
            return True

    def forget(self, key: str) -> bool:
        """把条目连同 meta 一起从索引层抹掉（**不含 bin**，bin 由 T-11 删）。

        顺序严格是「先摘 index → 再删 meta」。
        """
        removed = self.drop_from_index(key)
        with contextlib.suppress(OSError):
            self.meta_path(key).unlink(missing_ok=True)
        return removed

    # ---------------------------------------------------------------- 容量与 LRU

    def entries(self) -> list[IndexEntry]:
        """按 `last_used_at` **升序**返回（最久未用的在前）—— 正是 LRU 淘汰要的顺序。"""
        return sorted(self.load().values(), key=lambda entry: (entry.last_used_at, entry.key))

    def stats(self) -> IndexStats:
        """容量统计。`bytes_on_disk` 用真实文件大小（AC6 用它，别用自报值）。"""
        entries = self.load()
        on_disk = 0
        for key in entries:
            path = self.bin_path(key)
            with contextlib.suppress(OSError):
                on_disk += path.stat().st_size
        return IndexStats(
            entries=len(entries),
            bytes_claimed=sum(entry.bytes for entry in entries.values()),
            bytes_on_disk=on_disk,
        )

    # ---------------------------------------------------------------- 一致性修复

    def _scan_names(self) -> tuple[list[str], list[str]]:
        """列出目录里的 bin key 与 meta key。"""
        if not self.cache_dir.exists():
            return [], []
        bins: list[str] = []
        metas: list[str] = []
        for path in sorted(self.cache_dir.iterdir()):
            name = path.name
            if name.startswith(SLOTCACHE_PREFIX) and name.endswith(META_SUFFIX):
                metas.append(name[len(SLOTCACHE_PREFIX) : -len(META_SUFFIX)])
            elif name.startswith(SLOTCACHE_PREFIX) and name.endswith(BIN_SUFFIX):
                bins.append(name[len(SLOTCACHE_PREFIX) : -len(BIN_SUFFIX)])
        return bins, metas

    def scan_and_repair(self, *, dry_run: bool = False) -> RepairReport:
        """启动扫描：清孤儿 + 重建索引（幂等，可反复跑）。

        三类孤儿（T-08 验收清单点名）：

        1. **有 bin 无 meta** —— meta 没写成（进程死在 bin 与 meta 之间）。
           这份 KV **不可用**：不知道它的模型/量化/token 数，无法校验 REQ-U2，
           按 REQ-W2「宁可重算」直接清掉。
        2. **有 meta 无 bin** —— bin 没落盘或被外部删了。
        3. **meta 与 index 不一致** —— index 少了条目、多了条目，或缓存的
           `bytes`/`last_used_at` 落后。以 **meta.json 为权威**重建。

        ⚠️ 关于「不删 KV 文件本体」：正常读写路径确实不碰 bin（那是引擎/T-11 的事）。
        但孤儿 bin 属于**不可用**的残留 —— 不清理它就永远占着磁盘，且永远不可能被命中。
        这是本层唯一会删 bin 的地方，`dry_run=True` 可只报告不动手。

        **跨进程竞态兜不住**：如果另一个进程正在写，本函数可能把「正在写而 meta 还没落」
        的 bin 当孤儿删掉。见模块 docstring 的锁边界。
        """
        bins, metas = self._scan_names()
        existing = self._load_for_repair()

        orphan_bins: list[str] = []
        orphan_metas: list[str] = []
        valid: dict[str, CacheMeta] = {}

        for key in metas:
            meta = self._read_meta_for_repair(key)
            if meta is None or meta.key != key or not self.bin_path(key).exists():
                orphan_metas.append(key)
                continue
            valid[key] = meta

        for key in bins:
            if key not in valid:
                orphan_bins.append(key)

        repaired = sum(1 for key, meta in valid.items() if not _entry_matches(existing.get(key), meta)) + sum(
            1 for key in existing if key not in valid
        )

        if not dry_run:
            for key in orphan_metas:
                with contextlib.suppress(OSError):
                    self.meta_path(key).unlink(missing_ok=True)
            for key in orphan_bins:
                with contextlib.suppress(OSError):
                    self.bin_path(key).unlink(missing_ok=True)
            with self._index_lock:
                rebuilt = {
                    key: IndexEntry(
                        key=key,
                        meta_file=self.meta_path(key).name,
                        bytes=meta.bytes,
                        last_used_at=meta.last_used_at,
                    )
                    for key, meta in valid.items()
                }
                self._dump(rebuilt)

        return RepairReport(
            scanned_bins=len(bins),
            scanned_metas=len(metas),
            orphan_bins_removed=sorted(orphan_bins),
            orphan_metas_removed=sorted(orphan_metas),
            index_entries_repaired=repaired,
            index_rebuilt=not dry_run,
        )

    def _load_for_repair(self) -> dict[str, IndexEntry]:
        """修复场景下读索引：坏掉就当空（反正接下来要整体重建，且现场已被隔离）。"""
        try:
            return self.load()
        except IndexCorrupted:
            return {}

    def _read_meta_for_repair(self, key: str) -> CacheMeta | None:
        try:
            return self.read_meta(key)
        except (ValueError, OSError):
            return None


def _entry_matches(entry: IndexEntry | None, meta: CacheMeta) -> bool:
    if entry is None:
        return False
    return entry.bytes == meta.bytes and entry.last_used_at == meta.last_used_at


def iter_meta_paths(keys: Iterable[str], cache_dir: Path) -> list[Path]:
    """便捷函数：一批 key → meta 文件路径（报告与排查用）。"""
    return [Path(cache_dir) / f"{SLOTCACHE_PREFIX}{key}{META_SUFFIX}" for key in keys]


__all__ = [
    "BIN_SUFFIX",
    "INDEX_NAME",
    "INDEX_VERSION",
    "META_SUFFIX",
    "SLOTCACHE_PREFIX",
    "CacheIndex",
    "IndexEntry",
    "IndexStats",
    "RepairReport",
    "atomic_write_text",
    "iter_meta_paths",
]
