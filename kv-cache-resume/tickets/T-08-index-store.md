# T-08 · 索引与元数据存储（单写者）

| 项 | 值 |
| :--- | :--- |
| 状态 | ✅ 完成（2026-09-12） |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | T-05 |
| 阻塞 | T-09、T-10、T-11 |
| SPEC 依据 | REQ-U3、§4.3 元数据结构、§4.4 存储布局 |
| 预估 | 3~4 h |

## 要做出什么

磁盘上那套「KV 文件 + 元数据 + 全局索引」能被安全地读写：并发写不坏文件、进程被杀不留半截 JSON、索引与文件不会长期不一致。

写入顺序的一致性方向是本票的核心设计点（见备注），做反了会让 T-10 和 T-11 一起出问题。

## 范围

**做**

- `kv_cache/store.py`：`CacheIndex`
  - **文件布局**严格对齐 SPEC §4.4：
    ```
    <KV_CACHE_DIR>/
    ├── slotcache_{key}.bin
    ├── slotcache_{key}.meta.json
    └── index.json
    ```
  - 元数据 schema **逐字段**对齐 SPEC §4.3：`key` / `model_id` / `quant` / `n_tokens` / `bytes` / `created_at` / `last_used_at` / `hits`（`created_at` / `last_used_at` 用带时区的 ISO 8601，与 SPEC 示例一致）
  - **原子写**：`tmp` 文件 → `os.replace`。`index.json` 任何时刻都必须可被解析（不许半截 JSON）
  - **单写者纪律（REQ-U3）**：
    - 同 key 串行：进程内 per-key 锁
    - `index.json`：一把全局锁
    - **跨进程不做**（spike 定位）—— 但要在 docstring 里写清边界，别让后来人以为这是线程安全的万能实现
  - 容量统计：条目数 / 总字节数（供 T-11 用）
  - **启动时一致性修复**：扫描目录，清掉「有 bin 无 meta」「有 meta 无 bin」「有 meta 但 meta 与 index 不一致」三类孤儿，清理计数进遥测（T-12）
- `tests/test_store.py`
  - 并发写同一 key（同一进程，20 个任务）→ 最终文件可解析、内容为最后一次写
  - 并发写 `index.json`
  - 写 `tmp` 中途抛错 → 目录里不留半截文件
  - 孤儿三种形态各一个用例
  - meta schema 断言（字段名 + 类型 + 时区格式）

**不做**

- 不做 LRU 决策（T-11）
- 不做跨进程锁 / 文件锁（写清这是已知边界）
- 不删 KV 文件本体（本票只管索引层）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/kv_cache/store.py` | 索引 + 元数据存储 + 一致性修复 |
| `kv-cache-resume/tests/test_store.py` | 并发 / 中断 / 孤儿用例 |

## 验收清单

- [x] 并发 20 个任务写同一 key：最终文件可解析、内容自洽（REQ-U3）
      —— 见下方「一处口径修正」：并发的「最后一次写」不可观测，改用**更强**的判定性断言
- [x] `index.json` 在任意时刻可被读取（原子写生效，无半截 JSON 用例）
      —— `test_failure_midway_leaves_no_tmp_and_keeps_old_content`（模拟 replace 前崩溃：
      目标文件保持原样、无 tmp 残留）+ `test_failed_register_does_not_corrupt_index`
- [x] 三类孤儿在启动扫描时被清掉，且**清理计数可被遥测读到**
      —— `RepairReport` 带 `scanned_bins` / `scanned_metas` / `orphan_bins_removed` /
      `orphan_metas_removed` / `index_entries_repaired` / `total_cleaned`，T-12 直接读
- [x] meta schema 与 SPEC §4.3 逐字段一致（有 schema 断言测试，不是靠人眼看）
      —— `test_meta_matches_spec_schema_field_by_field` 断言字段集合**恰好相等**（多一个也失败）
- [x] 锁边界写进了 docstring：进程内有效、跨进程不保证
      —— 模块 docstring 的「锁边界」小节 + `CacheIndex` 类 docstring；`test_lock_boundary_documented` 守住
- [x] 时间戳带时区（`+08:00` 那种），不是裸 `datetime.isoformat()` 丢时区

## 一处口径修正：并发的「内容为最后一次写」改为**可判定**的等价断言

票面要求「并发 20 个任务写同一 key → 内容为最后一次写」。**这个断言在并发下不可判定**：
哪个线程最后落地没有可观测的全序，写这条断言只会得到一条随机红的用例。

所以拆成两条，合起来比原断言更强：

1. **`hits` 必须正好等于 20**（`test_concurrent_touch_same_key_does_not_lose_counts`）。
   `touch` 是「读 meta → 加一 → 写回」，锁失效就会**丢更新**、计数小于 20。
   这是 per-key 锁的**判定性证据** —— 而「文件能解析」在丢更新时照样通过。
2. **最终态内部自洽**：meta.json 必须**整体等于**某一次完整写入的结果（不能是拼接/撕裂的），
   且 index 的 `bytes` / `last_used_at` 与之一致；`index.json` 仍是合法 JSON
   （`test_concurrent_register_same_key_final_state_is_consistent`）。
   20 个不同 key 并发写则断言 20 条一条不丢（全局锁的判定性证据）。

`test_sequential_last_write_semantics` 单独钉住确定性场景：顺序写 5 次，
最终 meta 与 index 都必须是第 5 次的值（逐字段相等）——
否则「最后一次写胜出」这句话就完全没有测试在守。

## 落地时与票面不符之处（已回改文档）

1. **「不删 KV 文件本体」与「清掉有 bin 无 meta」在票面里互相矛盾**（前者在「不做」里，
   后者在「做」里）。裁定：正常读写路径**不碰 bin**（落盘归引擎、删除归 T-11）；
   **只有一致性修复会删 bin**，且孤儿 bin 属于不可用残留（无 meta → 不知模型/量化/token 数
   → 无法校验 REQ-U2 → 按 REQ-W2「宁可重算」），不清理就永远占磁盘且永不可能命中。
   已写入 `docs/phase1-design.md` §1.2 与 `store.py` 模块 docstring。
   另给了 `dry_run=True` 只报告不动手。
2. **新增错误类型 `IndexCorrupted`（改动了 T-05 的 `errors.py`）**：
   `index.json` 解析失败时**绝不静默当成空索引** —— 那会把所有 KV 文件一次变成孤儿，
   紧接着被启动扫描全删掉，**一次解析失败升级成一次删库**。改为：隔离坏文件留现场 + 显式报错，
   再由 `scan_and_repair()` 从各条目的 `meta.json` 重建。
3. **容量统计给两个口径**（`bytes_claimed` / `bytes_on_disk`）。AC6（目录占用 ≤ 上限）
   必须用 `bytes_on_disk`，否则等于拿「我以为写了多少」去比上限。已写入设计文档 §1.3。
4. **index 里不存绝对路径**：`meta_file` 只存纯文件名（存绝对路径会让整个缓存目录不可搬迁）。
5. **key 做了强校验**（`^[0-9a-f]{16}$`）：key 会被拼进文件名，
   `../x` 这种能直接穿越出缓存目录。`prefix.compute_key()` 天然满足形状，但存储层不假设调用方是它。

## 实现要点（供 T-09 / T-10 / T-11 直接调用）

| 方法 | 谁用 | 说明 |
| :--- | :--- | :--- |
| `register(meta)` | T-10 | 落盘第三步；**会校验 bin 已存在**，否则抛 `SaveFailed` |
| `get(key)` / `read_meta(key)` | T-09 | 走 `meta.json`（权威），不走 index 缓存 |
| `touch(key)` | T-09 | `hits + 1` + 刷新 `last_used_at`，持 per-key 锁故并发不丢计数 |
| `drop_from_index(key)` / `forget(key)` | T-11 | 删除流程第一步 / 第二步（`forget` 删 meta，**不删 bin**） |
| `entries()` | T-11 | 按 `last_used_at` **升序**（最久未用在前），每项只含 bytes/last_used_at，无需读文件 |
| `stats()` | T-11 | `entries` / `bytes_claimed` / `bytes_on_disk` |
| `scan_and_repair(dry_run=)` | 启动路径 | 幂等；`RepairReport` 供 T-12 遥测 |
| `from_config(config)` | 接入侧 | 从 `KVCacheConfig` 构造 |

## 备注

- **写入顺序一致性方向（本票最重要的设计点）**：
  - **落盘时**（T-10 用）：先写 `bin` → 再写 `meta` → 最后更新 `index`。中断最多留下孤儿文件，**绝不会有 index 指向不存在的文件**。
  - **删除时**（T-11 用）：**先摘 index 条目 → 再删文件**。同样是「中断只留孤儿」，方向一致。
  - 两个方向都指向同一句话：**`index.json` 是最后的真相源，它要么没有，要么指向真实存在的文件。** 孤儿由本票的启动扫描兜底回收。
- 别用 `json.dump` 直接写目标文件——那是最常见的半截 JSON 来源。`tmp` + `os.replace` 是硬要求。
- 本项目与 LexAgent `StreamEventLog` 同构（SPEC §4.1）：那边靠「仅 worker 线程写，所以 `LLEN+1` 不用锁」守住单写者纪律。本票的锁是对这条纪律的显式化，不是发明新东西。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-09](./T-09-hit-and-restore.md) / [T-10](./T-10-persist-and-save.md) / [T-11](./T-11-eviction-and-watermark.md)
