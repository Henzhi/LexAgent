# T-12 · 遥测口径（对齐 F15）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | T-09、T-10 |
| 阻塞 | T-13 |
| SPEC 依据 | G4、REQ-O2、§4.2 组件 3 |
| 预估 | 2~3 h |

## 要做出什么

每次请求产出一行**可机读**的遥测，字段口径与 LexAgent 的 F15 埋点对齐，能直接和线上数据对比。

这是 G4 的交付实质，也是 R7（投入产出比被质疑）的挡箭牌——**没有可度量的数字，这个 spike 就只是「我跑通了」**。

## 范围

**做**

- `kv_cache/telemetry.py`
  - 事件类型：`restore_hit` / `restore_miss` / `saved_bytes` / `restore_ms` / `cold_prefill_ms`（REQ-O2 逐字要求）+ `miss_reason`（T-09 已定义为枚举）
  - 输出：JSONL 追加写 `logs/`（已被 `.gitignore` 覆盖）+ 进程内计数器（便于测试断言）
  - **不引入新依赖**（用标准库 `json` / `time.perf_counter`）
  - 写失败 → 告警放行，**不影响主流程**（与 SPEC §4.1「日志故障只告警」同一纪律）
- **F15 口径对齐（本票最实质的工作）**
  - 读 LexAgent 的 `src/observability/usage_store.py` 与 `query_log.py`（**只读，不改**），确认：
    - 字段命名习惯（snake_case？前缀？）
    - 单位（tokens 是原始数还是千数、耗时 ms 还是 s、字节还是 MB）
    - cache hit/miss 的既有表达方式（如果有）
  - 在 `docs/phase1-design.md` 里产出一张**映射表**：本项目的字段 ↔ F15 字段 ↔ 单位 ↔ 语义差异说明
  - 有语义冲突时**显式写出来**，不要强行对齐（例如 F15 的 cache 是指云端前缀缓存，我们的是本地 KV 文件缓存，同名不同物必须注明）
- `tests/test_telemetry.py`
  - 三条决策路径（hit / miss / uncached）都产出事件，字段无缺
  - 单位正确（断言数值落在合理量级，防止把秒当毫秒写）
  - `miss_reason` 枚举全覆盖
  - 写失败不抛

**不做**

- 不做可视化面板（那是 LexAgent F15 的事）
- 不接入 LexAgent 的存储层（Phase 2 才谈）
- 不做采样/聚合（spike 阶段全量记录更值钱）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/kv_cache/telemetry.py` | 事件定义 + 输出 |
| `kv-cache-resume/tests/test_telemetry.py` | 字段 / 单位 / 枚举覆盖 |
| `docs/phase1-design.md` 补章 | **F15 字段映射表**（含同名不同物的显式说明） |

## 验收清单

- [ ] **REQ-O2**：`restore_hit` / `restore_miss` / `saved_bytes` / `restore_ms` / `cold_prefill_ms` 五个字段全部产出，一次请求一条完整记录
- [ ] F15 映射表逐字段说明对应关系与**单位**，同名不同物处有显式标注
- [ ] 缺失数据有明确表示（不是 0，而是 `null` 或字段缺席 —— 选择一种并写清），避免「0ms 到底是没有还是很快」的歧义
- [ ] 遥测写失败不影响主流程（断言不抛 + 告警被记录）
- [ ] 无新依赖（`pyproject.toml` 的依赖清单不变）
- [ ] 映射表引用了 LexAgent 侧的具体文件与字段名（可核对），不是泛泛而谈

## 备注

- **先读 F15 再写代码**，别先写完再对齐——单位错了（秒 vs 毫秒）事后极难发现，因为数字都「看起来挺合理」。
- 对齐的目的是**能横向比较**：用户最在意的是「本地 KV 复用在 ReAct 场景下的 cache miss tokens 相比线上是什么水平」（Phase 3 的目标）。映射表要把这条路铺好。
- 本票只读 LexAgent 侧代码，**不改任何 `src/` 文件**。若发现 F15 埋点本身有问题，记录下来交给后续，不在本 spike 里修。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-13](./T-13-acceptance-suite.md)
