"""`kv_cache.store` 的全离线单测（T-08）。

覆盖并发写（同 key / index）、原子写（中断不留半截）、三类孤儿、meta schema、
key 校验与锁边界。全部走临时目录，不依赖任何外部服务。

SPEC 依据：REQ-U3、§4.3（元数据结构）、§4.4（存储布局）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from kv_cache.errors import IndexCorrupted, SaveFailed
from kv_cache.models import CacheMeta, now_iso
from kv_cache.prefix import KEY_HEX_LEN, compute_key
from kv_cache.store import INDEX_NAME, INDEX_VERSION, CacheIndex, atomic_write_text

MODEL = "qwen2.5-3b-instruct"
QUANT = "q4_k_m"

#: 与 SPEC §4.3 的示例逐字段对齐 —— 少一个多一个都算 schema 漂移
SPEC_META_FIELDS = {"key", "model_id", "quant", "n_tokens", "bytes", "created_at", "last_used_at", "hits"}


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "kv"


@pytest.fixture
def index(cache_dir: Path) -> CacheIndex:
    return CacheIndex(cache_dir)


def a_key(seed: str = "a") -> str:
    """生成一个合法的 16 位十六进制 key（形状与 compute_key 一致）。"""
    return compute_key(MODEL, QUANT, list(seed.encode("utf-8")))


def make_meta(key: str, *, n_tokens: int = 100, nbytes: int = 1024, hits: int = 0) -> CacheMeta:
    stamp = now_iso()
    return CacheMeta(
        key=key,
        model_id=MODEL,
        quant=QUANT,
        n_tokens=n_tokens,
        bytes=nbytes,
        created_at=stamp,
        last_used_at=stamp,
        hits=hits,
    )


def write_bin(cache_dir: Path, key: str, size: int = 1024) -> Path:
    """造一个占位的 bin（本层只关心它存不存在、多大）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"slotcache_{key}.bin"
    path.write_bytes(b"\x00" * size)
    return path


def register_new(index: CacheIndex, cache_dir: Path, seed: str = "a", **kwargs) -> CacheMeta:
    key = a_key(seed)
    meta = make_meta(key, **kwargs)
    write_bin(cache_dir, key, size=kwargs.get("nbytes", 1024))
    index.register(meta)
    return meta


# ---- 布局与 schema ----------------------------------------------------------------


class TestLayoutAndSchema:
    def test_paths_follow_spec_layout(self, index: CacheIndex, cache_dir: Path):
        """SPEC §4.4：`slotcache_{key}.bin` / `slotcache_{key}.meta.json` / `index.json`。"""
        key = a_key()
        assert index.bin_path(key).name == f"slotcache_{key}.bin"
        assert index.meta_path(key).name == f"slotcache_{key}.meta.json"
        assert index.index_path.name == INDEX_NAME == "index.json"

    def test_paths_live_under_cache_dir(self, index: CacheIndex, cache_dir: Path):
        key = a_key()
        assert index.bin_path(key).parent == cache_dir
        assert index.meta_path(key).parent == cache_dir

    def test_meta_matches_spec_schema_field_by_field(self, index: CacheIndex, cache_dir: Path):
        """验收：meta 字段与 SPEC §4.3 逐字段一致（断言，不靠人眼）。"""
        meta = register_new(index, cache_dir)
        payload = json.loads(index.meta_path(meta.key).read_text(encoding="utf-8"))
        assert set(payload) == SPEC_META_FIELDS
        assert payload["key"] == meta.key
        assert isinstance(payload["n_tokens"], int)
        assert isinstance(payload["bytes"], int)
        assert isinstance(payload["hits"], int)
        assert isinstance(payload["model_id"], str)
        assert isinstance(payload["quant"], str)

    def test_timestamps_carry_timezone(self, index: CacheIndex, cache_dir: Path):
        """验收：时间戳带时区（不是裸 `datetime.isoformat()`）。"""
        meta = register_new(index, cache_dir)
        for field_name in ("created_at", "last_used_at"):
            parsed = datetime.fromisoformat(meta.to_dict()[field_name])
            assert parsed.tzinfo is not None, f"{field_name} 丢了时区"
            assert parsed.utcoffset() is not None

    def test_index_points_at_meta_filename_not_absolute_path(self, index: CacheIndex, cache_dir: Path):
        """index 里存纯文件名 —— 存绝对路径会让整个缓存目录不可搬迁。"""
        meta = register_new(index, cache_dir)
        entry = index.load()[meta.key]
        assert entry.meta_file == f"slotcache_{meta.key}.meta.json"
        assert "/" not in entry.meta_file and "\\" not in entry.meta_file

    def test_index_records_version_and_totals(self, index: CacheIndex, cache_dir: Path):
        register_new(index, cache_dir, nbytes=2048)
        payload = json.loads(index.index_path.read_text(encoding="utf-8"))
        assert payload["version"] == INDEX_VERSION
        assert payload["totals"]["entries"] == 1
        assert payload["totals"]["bytes"] == 2048


# ---- key 校验 ---------------------------------------------------------------------


class TestKeyValidation:
    @pytest.mark.parametrize("bad", ["", "../escape", "ABC", "z" * 16, "a" * 15, "a" * 17, "a/b"])
    def test_rejects_bad_key(self, index: CacheIndex, bad: str):
        with pytest.raises(ValueError, match="非法缓存 key"):
            index.bin_path(bad)

    def test_path_traversal_is_blocked(self, index: CacheIndex):
        """key 会被拼进文件名 —— 不校验就等于允许写出缓存目录。"""
        with pytest.raises(ValueError):
            index.meta_path("../../etc/passwd")

    def test_computed_keys_always_pass(self, index: CacheIndex):
        """`prefix.compute_key()` 的输出必须天然合法（跨模块契约）。"""
        key = compute_key(MODEL, QUANT, [1, 2, 3])
        assert len(key) == KEY_HEX_LEN
        assert index.bin_path(key).name.startswith("slotcache_")


# ---- 写入顺序与不变式 -------------------------------------------------------------


class TestWriteOrderInvariant:
    def test_register_requires_bin_to_exist(self, index: CacheIndex):
        """写入顺序 bin → meta → index；先写 index 会造出「索引指向不存在的文件」。"""
        meta = make_meta(a_key())
        with pytest.raises(SaveFailed, match="不存在"):
            index.register(meta)

    def test_register_then_get_roundtrip(self, index: CacheIndex, cache_dir: Path):
        meta = register_new(index, cache_dir, n_tokens=42, nbytes=4096)
        loaded = index.get(meta.key)
        assert loaded == meta

    def test_get_reads_meta_as_authority_not_index(self, index: CacheIndex, cache_dir: Path):
        """把 index 里的 bytes 改坏，`get()` 仍应给出 meta.json 里的真值。

        index 是**派生缓存**，meta.json 才是条目自身的权威记录 —— 这条区分是
        「第三类孤儿」能被检测出来的前提。
        """
        meta = register_new(index, cache_dir, nbytes=4096)
        payload = json.loads(index.index_path.read_text(encoding="utf-8"))
        payload["entries"][meta.key]["bytes"] = 999999
        index.index_path.write_text(json.dumps(payload), encoding="utf-8")

        assert index.get(meta.key).bytes == 4096
        assert index.load()[meta.key].bytes == 999999, "index 确实被改坏了，用来验证下一段"

    def test_missing_entry_returns_none(self, index: CacheIndex):
        assert index.get(a_key("nope")) is None

    def test_drop_from_index_leaves_files_alone(self, index: CacheIndex, cache_dir: Path):
        """删除流程第一步只摘索引，不碰文件（文件由 T-11 删）。"""
        meta = register_new(index, cache_dir)
        assert index.drop_from_index(meta.key) is True
        assert index.load() == {}
        assert index.meta_path(meta.key).exists()
        assert index.bin_path(meta.key).exists()

    def test_drop_returns_false_when_absent(self, index: CacheIndex):
        assert index.drop_from_index(a_key("absent")) is False

    def test_forget_removes_meta_but_keeps_bin(self, index: CacheIndex, cache_dir: Path):
        meta = register_new(index, cache_dir)
        assert index.forget(meta.key) is True
        assert not index.meta_path(meta.key).exists()
        assert index.bin_path(meta.key).exists(), "bin 不归本层删（T-11 的职责）"


# ---- 原子写 -----------------------------------------------------------------------


class TestAtomicWrite:
    def test_atomic_write_creates_content(self, tmp_path: Path):
        target = tmp_path / "x.json"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_atomic_write_creates_parent_dir(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "x.json"
        atomic_write_text(target, "hi")
        assert target.exists()

    def test_failure_midway_leaves_no_tmp_and_keeps_old_content(self, tmp_path: Path, monkeypatch):
        """验收：写 tmp 途中抛错 → **目录里不留半截文件**，且旧内容完好。

        模拟「内容已写进 tmp、replace 之前进程被杀」这个窗口。
        """
        target = tmp_path / "index.json"
        atomic_write_text(target, "OLD")

        import kv_cache.store as store_module

        def boom(*_args, **_kwargs):
            raise OSError("模拟 replace 之前崩溃")

        monkeypatch.setattr(store_module.os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(target, "NEW")

        assert target.read_text(encoding="utf-8") == "OLD", "目标文件必须保持原样"
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "index.json"]
        assert leftovers == [], f"残留了 tmp 文件：{leftovers}"

    def test_failed_register_does_not_corrupt_index(self, index: CacheIndex, cache_dir: Path, monkeypatch):
        """落盘链路中途失败时，`index.json` 仍是**可解析**的（不许半截 JSON）。"""
        register_new(index, cache_dir, seed="keep")
        before = index.index_path.read_text(encoding="utf-8")

        import kv_cache.store as store_module

        monkeypatch.setattr(store_module.os, "replace", lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
        key = a_key("second")
        write_bin(cache_dir, key)
        with pytest.raises(OSError):
            index.register(make_meta(key))

        assert index.index_path.read_text(encoding="utf-8") == before
        assert index.load()[a_key("keep")].bytes == 1024, "既有条目不受影响"


# ---- 并发 -------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_touch_same_key_does_not_lose_counts(self, index: CacheIndex, cache_dir: Path):
        """**per-key 锁的判定性证据**：20 个并发 `touch` 后 `hits` 必须正好是 20。

        `touch` 是「读 meta → 加一 → 写回」的读-改-写；锁要是没生效，就会丢更新，
        计数会小于 20。这条比「文件能解析」强得多 —— 后者在丢更新时也照样通过。
        """
        meta = register_new(index, cache_dir, hits=0)
        threads = [threading.Thread(target=index.touch, args=(meta.key,)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert index.get(meta.key).hits == 20
        assert index.load()[meta.key].bytes == meta.bytes

    def test_concurrent_register_same_key_final_state_is_consistent(self, index: CacheIndex, cache_dir: Path):
        """并发写同一 key：20 次写都成功，最终态**内部自洽**（meta 与 index 一致、文件可解析）。

        刻意不断言「等于第几次写的值」—— 并发下哪个线程最后落地没有可观测的全序，
        断言它只会得到一条随机红的用例。这里真正要守住的是**不撕裂**。
        """
        key = a_key("race")
        write_bin(cache_dir, key, size=1024)
        values = [make_meta(key, n_tokens=i + 1, nbytes=(i + 1) * 100) for i in range(20)]

        errors: list[BaseException] = []

        def worker(meta: CacheMeta) -> None:
            try:
                index.register(meta)
            except BaseException as exc:  # noqa: BLE001 —— 失败要带回去，不能在子线程里吞掉
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(meta,)) for meta in values]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        final = index.get(key)
        assert final in values, "最终 meta 必须是某一次完整写入的结果，不能是拼接出来的"
        entry = index.load()[key]
        assert entry.bytes == final.bytes
        assert entry.last_used_at == final.last_used_at
        # index.json 仍是合法 JSON（无半截）
        json.loads(index.index_path.read_text(encoding="utf-8"))

    def test_concurrent_register_different_keys_keeps_all_entries(self, index: CacheIndex, cache_dir: Path):
        """并发写 20 个不同 key：全局锁的判定性证据 —— 20 条都不能丢。"""
        keys = [a_key(f"k{i}") for i in range(20)]
        for key in keys:
            write_bin(cache_dir, key)

        errors: list[BaseException] = []

        def worker(key: str) -> None:
            try:
                index.register(make_meta(key))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert index.stats().entries == 20
        assert set(index.load()) == set(keys)

    def test_lock_boundary_documented(self):
        """验收：锁边界写进了 docstring（进程内有效、跨进程不保证）。"""
        doc = CacheIndex.__doc__ or ""
        assert "跨进程" in doc
        assert "进程内" in doc

    def test_sequential_last_write_semantics(self, index: CacheIndex, cache_dir: Path):
        """顺序写的「最后一次写胜出」是**确定性**的，这条用逐字段相等钉死。

        并发下这个断言不可判定（见类 docstring），所以顺序语义必须单独有确定性覆盖 ——
        否则「最后一次写胜出」这句话就完全没有测试在守。
        """
        key = a_key("seq")
        write_bin(cache_dir, key)
        for n_tokens in (1, 2, 3, 4, 5):
            index.register(make_meta(key, n_tokens=n_tokens, nbytes=n_tokens * 100))

        final = index.get(key)
        assert final.n_tokens == 5
        assert final.bytes == 500
        entry = index.load()[key]
        assert entry.bytes == 500
        assert entry.last_used_at == final.last_used_at


# ---- 容量统计 ---------------------------------------------------------------------


class TestStats:
    def test_two_byte_measures_are_distinct(self, index: CacheIndex, cache_dir: Path):
        """**口径必须分开**：自报 `bytes` 与真实磁盘占用是两回事。

        AC6（目录占用 ≤ 上限）要用 `bytes_on_disk` —— 拿自报值比上限，
        等于用「我以为写了多少」去比上限。
        """
        key = a_key("sizes")
        meta = make_meta(key, nbytes=999_999)  # 自报一个与实际不符的值
        write_bin(cache_dir, key, size=2048)
        index.register(meta)

        stats = index.stats()
        assert stats.entries == 1
        assert stats.bytes_claimed == 999_999
        assert stats.bytes_on_disk == 2048

    def test_entries_sorted_for_lru(self, index: CacheIndex, cache_dir: Path):
        """`entries()` 按 `last_used_at` 升序（最久未用在前）—— 正是 LRU 要的顺序。"""
        stamps = ["2026-09-12T10:00:03+08:00", "2026-09-12T10:00:01+08:00", "2026-09-12T10:00:02+08:00"]
        keys = [a_key(f"lru{i}") for i in range(3)]
        for key, stamp in zip(keys, stamps, strict=True):
            write_bin(cache_dir, key)
            index.register(
                CacheMeta(
                    key=key,
                    model_id=MODEL,
                    quant=QUANT,
                    n_tokens=1,
                    bytes=1024,
                    created_at=stamp,
                    last_used_at=stamp,
                )
            )
        ordered = [entry.last_used_at for entry in index.entries()]
        assert ordered == sorted(stamps)

    def test_stats_on_empty_dir(self, index: CacheIndex):
        stats = index.stats()
        assert (stats.entries, stats.bytes_claimed, stats.bytes_on_disk) == (0, 0, 0)


# ---- 孤儿与一致性修复 -------------------------------------------------------------


class TestOrphansAndRepair:
    def test_orphan_bin_without_meta_is_removed(self, index: CacheIndex, cache_dir: Path):
        """第 1 类：有 bin 无 meta —— 不知道模型/量化/token 数，无法校验 REQ-U2，按 REQ-W2 清掉。"""
        key = a_key("orphanbin")
        write_bin(cache_dir, key)

        report = index.scan_and_repair()

        assert report.orphan_bins_removed == [key]
        assert not index.bin_path(key).exists()
        assert report.total_cleaned == 1

    def test_orphan_meta_without_bin_is_removed(self, index: CacheIndex, cache_dir: Path):
        """第 2 类：有 meta 无 bin。"""
        key = a_key("orphanmeta")
        cache_dir.mkdir(parents=True, exist_ok=True)
        index.meta_path(key).write_text(json.dumps(make_meta(key).to_dict()), encoding="utf-8")

        report = index.scan_and_repair()

        assert report.orphan_metas_removed == [key]
        assert not index.meta_path(key).exists()

    def test_all_three_orphan_kinds_in_one_pass(self, index: CacheIndex, cache_dir: Path):
        """三类孤儿同场：孤儿 bin / 孤儿 meta / meta 与 index 不一致。"""
        good = register_new(index, cache_dir, seed="good", nbytes=1024)

        orphan_bin = a_key("obin")
        write_bin(cache_dir, orphan_bin)

        orphan_meta = a_key("ometa")
        index.meta_path(orphan_meta).write_text(json.dumps(make_meta(orphan_meta).to_dict()), encoding="utf-8")

        # 第 3 类：把 index 里 good 的 bytes 改坏
        payload = json.loads(index.index_path.read_text(encoding="utf-8"))
        payload["entries"][good.key]["bytes"] = 42
        index.index_path.write_text(json.dumps(payload), encoding="utf-8")

        report = index.scan_and_repair()

        assert report.orphan_bins_removed == [orphan_bin]
        assert report.orphan_metas_removed == [orphan_meta]
        assert report.index_entries_repaired == 1, "good 的 index 缓存值落后，应被修复"
        assert report.scanned_bins == 2
        assert report.scanned_metas == 2
        assert index.load()[good.key].bytes == 1024, "以 meta.json 为权威重建"
        assert index.get(good.key).n_tokens == good.n_tokens

    def test_index_missing_entries_are_restored_from_metas(self, index: CacheIndex, cache_dir: Path):
        """第 3 类的另一种形态：index 整个丢了，但 meta 都在 → 应当被重建而不是清掉。"""
        first = register_new(index, cache_dir, seed="m1")
        second = register_new(index, cache_dir, seed="m2")
        index.index_path.unlink()

        report = index.scan_and_repair()

        assert report.index_entries_repaired == 2
        assert set(index.load()) == {first.key, second.key}
        assert report.orphan_bins_removed == []
        assert report.orphan_metas_removed == []

    def test_repair_is_idempotent(self, index: CacheIndex, cache_dir: Path):
        """扫两遍结果相同（幂等）—— 启动扫描会被反复调用。"""
        register_new(index, cache_dir, seed="stable")
        write_bin(cache_dir, a_key("junk"))

        first = index.scan_and_repair()
        second = index.scan_and_repair()

        assert first.orphan_bins_removed == [a_key("junk")]
        assert second.orphan_bins_removed == []
        assert second.total_cleaned == 0
        assert second.scanned_bins == 1

    def test_dry_run_reports_without_touching_files(self, index: CacheIndex, cache_dir: Path):
        key = a_key("dryrun")
        write_bin(cache_dir, key)

        report = index.scan_and_repair(dry_run=True)

        assert report.orphan_bins_removed == [key]
        assert report.index_rebuilt is False
        assert index.bin_path(key).exists(), "dry_run 不许动文件"

    def test_meta_whose_key_disagrees_with_filename_is_orphan(self, index: CacheIndex, cache_dir: Path):
        """meta 里的 key 与文件名对不上 —— 说明这一对已经错位，不能信。"""
        key = a_key("mismatch")
        write_bin(cache_dir, key)
        wrong = make_meta(a_key("other"))
        index.meta_path(key).write_text(json.dumps(wrong.to_dict()), encoding="utf-8")

        report = index.scan_and_repair()

        assert report.orphan_metas_removed == [key]
        assert report.orphan_bins_removed == [key], "bin 也随之失去归属"

    def test_unparseable_meta_is_orphan(self, index: CacheIndex, cache_dir: Path):
        key = a_key("badmeta")
        write_bin(cache_dir, key)
        index.meta_path(key).write_text("{ 这不是 JSON", encoding="utf-8")

        report = index.scan_and_repair()

        assert report.orphan_metas_removed == [key]

    def test_scan_on_missing_dir_is_noop(self, index: CacheIndex):
        report = index.scan_and_repair()
        assert report.scanned_bins == 0
        assert report.scanned_metas == 0


# ---- 坏索引 -----------------------------------------------------------------------


class TestCorruptIndex:
    def test_corrupt_index_raises_and_is_quarantined(self, index: CacheIndex, cache_dir: Path):
        """**不许静默当成空索引** —— 那会让所有 KV 文件变孤儿后被清理。"""
        cache_dir.mkdir(parents=True, exist_ok=True)
        index.index_path.write_text('{"version": 1, "entries": {', encoding="utf-8")

        with pytest.raises(IndexCorrupted) as excinfo:
            index.load()

        assert "scan_and_repair" in str(excinfo.value)
        quarantined = list(cache_dir.glob(f"{INDEX_NAME}.corrupt-*"))
        assert len(quarantined) == 1, "坏文件要留现场"
        assert not index.index_path.exists()

    def test_wrong_version_is_rejected(self, index: CacheIndex, cache_dir: Path):
        cache_dir.mkdir(parents=True, exist_ok=True)
        index.index_path.write_text(json.dumps({"version": 999, "entries": {}}), encoding="utf-8")
        with pytest.raises(IndexCorrupted):
            index.load()

    def test_corrupt_index_can_be_rebuilt_from_metas(self, index: CacheIndex, cache_dir: Path):
        """坏索引的出路：从各条目 meta.json 重建，KV 文件一条都不该丢。"""
        meta = register_new(index, cache_dir)
        index.index_path.write_text("not json at all", encoding="utf-8")

        report = index.scan_and_repair()

        assert report.orphan_bins_removed == []
        assert index.load()[meta.key].bytes == meta.bytes

    def test_missing_index_is_fine(self, index: CacheIndex, cache_dir: Path):
        """文件不存在 ≠ 损坏：首次运行就该是空索引。"""
        cache_dir.mkdir(parents=True, exist_ok=True)
        assert index.load() == {}


# ---- from_config ------------------------------------------------------------------


class TestFromConfig:
    def test_from_config_uses_cache_dir(self, make_config, tmp_path: Path):
        config = make_config(cache_dir=tmp_path / "fromcfg")
        index = CacheIndex.from_config(config)
        assert index.cache_dir == tmp_path / "fromcfg"
