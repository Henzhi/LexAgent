# T-08 · 索引与元数据存储（单写者）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
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

- [ ] 并发 20 个任务写同一 key：最终文件可解析、内容为最后一次写（REQ-U3）
- [ ] `index.json` 在任意时刻可被读取（原子写生效，无半截 JSON 用例）
- [ ] 三类孤儿在启动扫描时被清掉，且**清理计数可被遥测读到**
- [ ] meta schema 与 SPEC §4.3 逐字段一致（有 schema 断言测试，不是靠人眼看）
- [ ] 锁边界写进了 docstring：进程内有效、跨进程不保证
- [ ] 时间戳带时区（`+08:00` 那种），不是裸 `datetime.isoformat()` 丢时区

## 备注

- **写入顺序一致性方向（本票最重要的设计点）**：
  - **落盘时**（T-10 用）：先写 `bin` → 再写 `meta` → 最后更新 `index`。中断最多留下孤儿文件，**绝不会有 index 指向不存在的文件**。
  - **删除时**（T-11 用）：**先摘 index 条目 → 再删文件**。同样是「中断只留孤儿」，方向一致。
  - 两个方向都指向同一句话：**`index.json` 是最后的真相源，它要么没有，要么指向真实存在的文件。** 孤儿由本票的启动扫描兜底回收。
- 别用 `json.dump` 直接写目标文件——那是最常见的半截 JSON 来源。`tmp` + `os.replace` 是硬要求。
- 本项目与 LexAgent `StreamEventLog` 同构（SPEC §4.1）：那边靠「仅 worker 线程写，所以 `LLEN+1` 不用锁」守住单写者纪律。本票的锁是对这条纪律的显式化，不是发明新东西。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-09](./T-09-hit-and-restore.md) / [T-10](./T-10-persist-and-save.md) / [T-11](./T-11-eviction-and-watermark.md)
