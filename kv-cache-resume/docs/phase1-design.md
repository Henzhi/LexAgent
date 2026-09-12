# Phase 1 设计 · KV 缓存策略层

| 项 | 值 |
| :--- | :--- |
| 状态 | **进行中**（T-05 已落地骨架；T-06~T-13 分票填充） |
| 需求真相源 | [`../SPEC.md`](../SPEC.md) —— 本文档与 SPEC 冲突时以 SPEC 为准 |
| 执行单元 | [`../tickets/`](../tickets/README.md) |
| 隔离边界 | 全部代码在 `kv-cache-resume/` 内，**不 import 也不修改 LexAgent `src/`** |

> 本文档由各票**追加章节**，T-13 合并定稿。凡标注「T-xx 补章」的章节，在该票完成前内容为占位。

---

## 1. 模块职责边界（T-05）

一句话原则：**决策与 IO 分离**。策略层只做选择，HTTP 与磁盘各有专人。

| 模块 | 职责 | **不做** | 落地票 |
| :--- | :--- | :--- | :--- |
| `policy.py`<br>`KVCachePolicy` | 决策中枢：`lookup` / `persist` / `enforce_limits`；持有 per-key 锁 | 不发 HTTP、不开文件、不做重试 | T-09 / T-10 / T-11 |
| `engine.py`<br>`LlamaSlotClient` | **唯一**碰 HTTP 的地方：`health` / `capabilities` / `save` / `restore` | 不做降级决策（那是 policy 的事） | T-06 |
| `prefix.py` | 键计算 `compute_key`；归一化 `canonicalize`；不稳定字段检测 | 不实现真实 tokenizer（词表属接入侧） | T-07 |
| `store.py`<br>`CacheIndex` | 磁盘布局：`bin` / `meta` / `index.json`；原子写；单写者；孤儿回收 | 不做 LRU 决策（那是 eviction 的事） | T-08 |
| `eviction.py` | LRU 淘汰 + 磁盘水位状态机 | 不决定「何时该淘汰」（由 policy 在 persist 前触发） | T-11 |
| `telemetry.py` | 事件定义与 JSONL 输出 | 不做聚合 / 可视化（那是 LexAgent F15 的事） | T-12 |
| `config.py` | `KV_*` 环境变量与校验 | 不做进程级缓存（改了 env 就立即生效） | T-05 ✅ |
| `errors.py` | 错误分类，**降级逻辑的分支依据** | 不做字符串匹配 | T-05 ✅ |
| `models.py` | 跨模块数据结构：`Decision` / `CacheMeta` / `TelemetryEvent` | 不含任何行为逻辑 | T-05 ✅ |

### 1.1 数据流（对齐 SPEC §4.2）

```
请求(token 序列)
      │
      ▼
┌──────────────────────────────┐
│ policy.lookup()    [决策]     │
│  1. prefix.compute_key()     │
│  2. store 查索引 → 命中?       │
└───┬──────────────────────┬───┘
    │ HIT                  │ MISS / UNCACHED
    ▼                      ▼
┌────────────────┐  ┌─────────────────┐
│ engine.restore │  │ 冷 prefill      │
│ （装 KV 到 slot）│  │ （正常推理）      │
└───────┬────────┘  └────────┬────────┘
        └─────────┬──────────┘
                  ▼
         ┌─────────────────┐
         │  decode 继续生成  │
         └────────┬────────┘
                  ▼
         ┌──────────────────────────────────┐
         │ policy.persist()                  │
         │  eviction.enforce_limits() 前置检查 │
         │  engine.save → store 写 meta+index │
         └────────┬─────────────────────────┘
                  ▼
         ┌─────────────────┐
         │ telemetry 一行   │
         └─────────────────┘
```

---

### 1.2 磁盘一致性方向（T-08 核心设计点）

`index.json` 是最后的真相源，守住一句话：**它要么没有某个 key，要么指向真实存在的文件。**

| 流程 | 顺序 | 中断的后果 |
| :--- | :--- | :--- |
| **落盘**（T-10） | `bin` → `meta` → `index` | 最多留「孤儿 bin」或「孤儿 meta」 |
| **删除**（T-11） | 摘 `index` 条目 → 删文件 | 同上 |

两个方向都指向「中断只留孤儿」，所以**只有一种修复动作**：启动扫描回收孤儿。
反过来做（先写 index 再写文件）会造出「索引指向不存在的文件」，而那个状态无法靠扫描区分
「文件还没写完」与「文件被外部删了」，只能整库重建。

幂等性：`scan_and_repair()` 可反复调用，第二遍必为空操作 —— 它是启动路径，不是一次性脚本。

| 三类孤儿 | 判定 | 处置 | 依据 |
| :--- | :--- | :--- | :--- |
| 有 bin 无 meta | meta 没写成（进程死在 bin 与 meta 之间） | 删 bin | 不知模型/量化/token 数 → 无法校验 REQ-U2 → 按 REQ-W2「宁可重算」 |
| 有 meta 无 bin | bin 没落盘或被外部删除 | 删 meta | 文件不存在就没有可恢复的东西 |
| meta 与 index 不一致 | index 少条目 / 多条目 / 缓存字段落后 | 以 **meta.json 为权威**重建 | index 是**派生缓存**，条目自身的真相在 meta |

> ⚠️ 两处边界，写在这里免得后来人误读：
> 1. **本层只在一致性修复时删 bin**。正常读写路径不碰 bin（落盘归引擎、删除归 T-11）——
>    T-08 票面「不删 KV 文件本体」指的是这个。孤儿 bin 属于不可用残留，不清理就永远占磁盘且永不可能命中。
> 2. **锁只在进程内有效，跨进程不保证**。扫描兜得住「已坏掉的孤儿」，兜不住「两个进程同时写」的竞态。
>    跨进程需要文件锁，本票刻意不做（T-08「不做」）。

### 1.3 容量口径：自报值与真实占用必须分开

| 口径 | 含义 | 谁用 |
| :--- | :--- | :--- |
| `bytes_claimed` | 各条目 meta 自报的 `bytes` 之和 | 遥测（REQ-O2 的 `saved_bytes`） |
| `bytes_on_disk` | bin 文件**实际** `st_size` 之和 | **AC6**（目录占用 ≤ `KV_MAX_BYTES`） |

混用会让 AC6 变成「拿我以为写了多少去比上限」。两者的差值本身就是有用的诊断信号
（差得多说明有孤儿或外部改动）。

---

## 2. 命中判断状态机（T-05 核心交付）

本图是 T-09（命中判定）与 T-10（落盘）的施工图。**每个出口都必须有测试覆盖，不留「等等」式未定义节点**（T-09 验收清单最后一条）。

```mermaid
flowchart TD
    START([请求进入: token_ids + model_id + quant]) --> ENABLED{KV_ENABLED?}

    ENABLED -- 否 --> UNCACHED_DISABLED[UNCACHED<br/>detail: 缓存已关闭]
    ENABLED -- 是 --> CAP{服务端有 slot 落盘能力?<br/>capabilities 未缓存则探测一次}

    CAP -- 501 SlotApiUnavailable --> UNCACHED_501[UNCACHED<br/>detail: 未配 --slot-save-path]
    CAP -- 是 --> KEY[prefix.compute_key]

    KEY --> STABLE{含不稳定字段?<br/>时间戳 / 随机 ID}
    STABLE -- 是 --> MISS_PREFIX_REFUSE[MISS<br/>prefix_mismatch<br/>拒绝缓存该请求]
    STABLE -- 否 --> LOOKUP[store 查索引]

    LOOKUP --> FOUND{索引有该 key?}
    FOUND -- 否 --> MISS_NOTFOUND[MISS<br/>not_found]
    FOUND -- 是 --> VERIFY{meta.model_id / quant 匹配?}

    VERIFY -- 模型不符 --> DISCARD_M[MISS<br/>model_mismatch<br/>丢弃条目: 删文件 + 摘索引]
    VERIFY -- 量化不符 --> DISCARD_Q[MISS<br/>quant_mismatch<br/>丢弃条目: 删文件 + 摘索引]
    VERIFY -- 匹配 --> RESTORE[engine.restore<br/>受 RESTORE_TIMEOUT_S 约束]

    RESTORE --> RRESULT{结果?}
    RRESULT -- 501 --> UNCACHED_501
    RRESULT -- 超时 --> MISS_TO[MISS<br/>restore_timeout]
    RRESULT -- 非 2xx / 其它异常 --> MISS_RF[MISS<br/>restore_failed]
    RRESULT -- 200 --> PREFIXCHECK{token 序列校验通过?}

    PREFIXCHECK -- 否 --> MISS_PM[MISS<br/>prefix_mismatch]
    PREFIXCHECK -- 是 --> HIT[HIT filename<br/>hits+1, last_used_at 前进]

    HIT --> TELE_HIT[(遥测: restore_hit / restore_ms)]
    MISS_NOTFOUND --> TELE_MISS[(遥测: restore_miss / miss_reason)]
    MISS_PREFIX_REFUSE --> TELE_MISS
    DISCARD_M --> TELE_MISS
    DISCARD_Q --> TELE_MISS
    MISS_TO --> TELE_MISS
    MISS_RF --> TELE_MISS
    MISS_PM --> TELE_MISS
    UNCACHED_DISABLED --> TELE_UNC[(遥测: restore_uncached)]
    UNCACHED_501 --> TELE_UNC

    HIT --> DECODE([decode 继续生成])
    MISS_NOTFOUND --> COLD([冷 prefill])
    MISS_PREFIX_REFUSE --> COLD
    DISCARD_M --> COLD
    DISCARD_Q --> COLD
    MISS_TO --> COLD
    MISS_RF --> COLD
    MISS_PM --> COLD
    UNCACHED_DISABLED --> COLD
    UNCACHED_501 --> COLD
```

### 2.1 出口分支清点（T-09 逐条对表）

| # | 出口 | `DecisionKind` | reason | 依据 |
| ---: | :--- | :--- | :--- | :--- |
| 1 | 缓存已关闭 | `UNCACHED` | — | `KV_ENABLED=false` |
| 2 | 服务端无能力（501） | `UNCACHED` | — | REQ-E3 |
| 3 | 不稳定字段 → 拒绝缓存 | `MISS` | `prefix_mismatch` | REQ-W2、T-07 |
| 4 | 索引无此 key | `MISS` | `not_found` | REQ-E2 |
| 5 | 模型不符 → 丢弃条目 | `MISS` | `model_mismatch` | REQ-U2 |
| 6 | 量化不符 → 丢弃条目 | `MISS` | `quant_mismatch` | REQ-U2 |
| 7 | restore 超时 | `MISS` | `restore_timeout` | REQ-W1、AC5 |
| 8 | restore 非 2xx / 异常 | `MISS` | `restore_failed` | REQ-W1、AC5 |
| 9 | token 序列校验失败 | `MISS` | `prefix_mismatch` | REQ-W2 |
| 10 | **HIT** | `HIT` | — | REQ-E2、REQ-E4 |

> **共同纪律**：3~9 号出口**一律不抛异常**，全部回退冷 prefill 把请求做完（REQ-W1）。
> 「失败不抛」不等于「失败静默」—— 每个 MISS 都带 reason 且进告警日志。

---

## 3. 配置清单（T-05）

`KV_*` 全部集中在 `config.py`，每个都有默认值；**非法值抛 `KVCacheConfigError`，不静默取默认**。

| 环境变量 | 默认值 | 语义 | 依据 |
| :--- | :--- | :--- | :--- |
| `KV_ENABLED` | `true` | 缓存总开关 | T-05 |
| `KV_CACHE_DIR` | `kv` | 缓存目录（相对路径以 `kv-cache-resume/` 为基准） | SPEC §4.4 |
| `KV_MAX_BYTES` | `2GiB` | 目录占用上限（**保守占位，待 T-04 校准**） | REQ-S1、AC6 |
| `KV_MAX_ENTRIES` | `64` | 条目数上限（**保守占位，待 T-04 校准**） | REQ-S2 |
| `KV_MIN_FREE_BYTES` | `1GiB` | 磁盘水位下限；`0` = 关闭水位保护 | REQ-W4 |
| `KV_QUANT` | `f16` | KV 精度：`f16` / `q8` / `q4`，记入元数据 | REQ-O3 |
| `VERIFY_EXACT` | `false` | 逐字比对验收模式（默认关闭，开着显著变慢） | REQ-O1 |
| `RESTORE_TIMEOUT_S` | `10` | restore 超时上限 | REQ-W1 |
| `SERVER_BASE_URL` | `http://127.0.0.1:8080` | `llama-server` 地址 | T-06 |
| `KV_CAPABILITY_TTL_S` | `300` | 能力探测结论的缓存窗口；`0` = 每次都重探 | T-06 追加 |

> `KV_CAPABILITY_TTL_S` 是 T-06 需要而 T-05 清单未列的追加项（能力探测不能每次请求都发一次 RTT）。
> R6 的「上游 API 未承诺稳定」靠这个窗口兜：探测结论有失效期，引擎升级后不会永久错下去。

---

## 4. 归一化清单（T-07）

> 由 [T-07](../tickets/T-07-prefix-key.md) 补齐。T-13 验收时按本清单逐条构造样本（AC7）。
> **本清单与实际实现严格一一对应**（`kv_cache/prefix.py`），多一条少一条都算 T-07 未完成。

### 4.0 先划一条界：归一化改写「输入」，绝不改写「键」

AC7 要求「扰动 → 判 miss」，R1 的缓解措施要求「对不稳定字段做归一化」，两者表面冲突。
本层的处置是把它们放到**不同的层**：

| | 归一化（`canonicalize`） | 键（`compute_key`） |
| :--- | :--- | :--- |
| 作用对象 | 消息内容（**发送前**） | token 序列（**已被发送的那份**） |
| 是否默认启用 | **opt-in**（`canonical=True`） | 必走 |
| 谁改动了什么 | 把等价的写法折叠成同一份字节 | 一个 token 都不动 |

**关键约束**：采用 `canonicalize` 的调用方，必须把它的输出**当作真正要发送的内容**去 tokenize 并请求。
只归一化键而不改发送内容，会让键与「服务端那份 KV」脱钩 —— restore 返回 200、答案却是错的，而且**静默**。
这就是 REQ-W2 要防的事。

于是 AC7 与 R1 同时成立：扰动到达 token 层必然 miss（键不归一化）；而等价写法在上游被折叠后
产生的是一份**逐字节相同**的输入，命中的确实是同一段前缀的 KV。

### 4.1 被归一化的字段与规则（共 7 条，无遗漏）

| # | 规则 | 触发条件 | 代码位置 |
| :--- | :--- | :--- | :--- |
| 1 | `\r\n` / `\r` → `\n` | 任意字符串 | `normalize_whitespace` 规则 1 |
| 2 | 每行去掉行尾空格与 tab | 任意字符串 | `normalize_whitespace` 规则 2 |
| 3 | 连续 ≥3 个换行折成 2 个 | 任意字符串 | `normalize_whitespace` 规则 3 |
| 4 | 去掉整串首尾空白 | 任意字符串 | `normalize_whitespace` 规则 4 |
| 5 | dict 的 key 按字典序重排 | 任意 `Mapping` 值 | `_canonical_value` 的 Mapping 分支 |
| 6 | JSON 序列化统一为 `sort_keys=True, separators=(",",":"), ensure_ascii=False` | 值为 dict / list | `canonical_json` |
| 7 | 字符串字段里的 JSON 被解析、重排后回写 | 字段名 ∈ `{arguments, parameters, schema, input, args}` **且**能 `json.loads` 成 object/array | `_JSON_STRING_FIELDS` 分支 |

> 规则 7 的字段名清单是**数据**（`_JSON_STRING_FIELDS` 常量），加减字段只改常量。
> 列表元素沿用其字段名的提示，故 `arguments: ["{...}", "{...}"]` 这种数组形态同样被识别。

### 4.2 明确**不做**的归一化（宁可判 miss，也不改坏内容）

| 不做的事 | 为什么 |
| :--- | :--- |
| 不合并行内多个空格 | 行内空格在模板渲染里可能是语义的一部分 |
| 不删行首缩进 | 同上；缩进常被用来承载结构信息（YAML / 代码片段） |
| 保留 ≤2 个换行（只折 ≥3） | 「空一行」是有意的分段，抹掉它就改变了输入 |
| **不重排自由文本 `content` 里内嵌的 JSON** | 散文里哪一节算 JSON 无从判断，硬猜会把文本改坏。代价是该形态下 key 顺序变化仍判 miss —— 方向安全（多算一次，不会用错 KV）。要拿收益就把结构化内容放进 `arguments` 这类字段 |
| **不改写 token 序列** | 键必须与服务端那份 KV 严格对应（见 §4.0） |
| **不折叠 `quant` 的大小写** | 折叠方向是反的：会让 `Q4_K_M` 与 `q4_k_m` 共用键。大小写统一由 `config.KV_QUANT_CHOICES` 负责 |

### 4.3 被**拒绝缓存**的字段（检测到即抛 `PrefixMismatch`）

规则是数据：`UNSTABLE_PATTERNS`（`kv_cache/prefix.py`）。加规则只改元组，不动函数。

| kind | 匹配要点 | 为什么归为「不可缓存」 |
| :--- | :--- | :--- |
| `iso8601_datetime` | `YYYY-MM-DD` + **必须带时间** `HH:MM[:SS[.fff]][Z\|±HH:MM]` | 「当前时间」类注入的典型形态，两次请求之间必然不同 |
| `uuid` | 标准 8-4-4-4-12 形态 | 请求 id / trace id |
| `hex_id` | 连续 ≥16 位十六进制，**且必须含 a-f 字母** | 随机指纹 / 会话 id；要求含字母是为了排除纯数字长串 |
| `memory_address` | `0x` + ≥6 位十六进制 | Python 默认 `repr`（`<obj at 0x7f...>`）混进 prompt 时必然每次不同 |

命中后**不是**归一化掉，而是拒绝缓存该请求（`REQ-W2` 的「宁可重算」）：这些字段一变，
token 序列就真的变了，旧 KV 对应的不是这次的前缀。显式 `check_unstable=False` 可承担该风险。

**被明确否决的规则候选（记录理由，避免将来有人再想加）**

- **裸 10/13 位 epoch 数字**：误报面太大 —— 法律语料里 11 位手机号、18 位身份证号、
  长编号比比皆是。误报会让本该命中的请求全部拒绝缓存，**直接打穿 AC4（命中率 ≥99%）**。
  口径纪律：「宁可重算」只是慢一次，「宁可不缓存」是收益归零 —— 两者不能混为一谈。
- **裸日期**（`2020-01-01`）：合同签订日、法条生效日这类**几乎全是内容**而不是注入的当前时间，
  纳入检测同样误伤 AC4。故 `iso8601_datetime` 强制要求带时间部分。
- **纯数字长串**（手机号 / 身份证号 / 案号）：同样是内容而非噪声，由 `hex_id` 的「必须含 a-f」排除。

### 4.4 键的编码（供 T-13 复核）

`sha256(model_id ‖ quant ‖ token_ids)[:16]`（REQ-U1、SPEC §4.3）。编码细节：

- 三段各自 **4 字节小端长度前缀 + 原文** —— 去掉长度前缀，`("ab","c")` 与 `("a","bc")`
  会拼出同一串字节、算出同一个键（两个模型共用一份 KV）。
- token id 用 **无符号 32 位小端**，与平台字节序无关；`bool` 显式拒绝
  （`isinstance(True, int)` 为真，会悄悄变成 token `1`）。
- 开头有编码版本标签 `kvprefix\x00v1` —— 改编码必须换标签，否则新旧编码可能算出同一个键。
- 16 位十六进制 = 64 bit；单机几万条目量级下碰撞概率可忽略（n=1e5 时约 2.7e-10）。

> `tokenize(messages)` 的签名归本层，**词表归调用方**（`MessageTokenizer` Protocol）。
> `CanonicalJsonByteTokenizer` 是**测试替身**，只保证确定性与单射，绝不能拿去和真实服务端对话。

---

## 5. 淘汰顺序与水位状态机（T-11 补章 · 待填）

> 由 [T-11](../tickets/T-11-eviction-and-watermark.md) 补齐：LRU 淘汰流程图 + 水位状态转换图。

---

## 6. F15 字段映射表（T-12 补章 · 待填）

> 由 [T-12](../tickets/T-12-telemetry.md) 补齐：本项目字段 ↔ LexAgent F15 字段 ↔ 单位 ↔ 语义差异。
> **同名不同物必须显式标注**：F15 的 cache 指云端前缀缓存，本项目是本地 KV 文件缓存。

---

## 7. 变更记录

| 日期 | 变更 | 票 |
| :--- | :--- | :--- |
| 2026-09-12 | 初版：模块边界表 + 数据流图 + 命中判断状态机（10 个出口）+ 配置清单 | T-05 |
| 2026-09-12 | §4 补齐：归一化清单（7 条规则 + 6 条明确不做 + 4 条拒绝缓存规则）；**新增 §4.0 划清「归一化改输入 / 从不已改写键」的界** —— 这是 AC7（扰动必须 miss）与 R1（空白归一）表面冲突的处置方式；记录 3 条被否决的检测规则及其理由（裸 epoch / 裸日期 / 纯数字长串，都是为避免打穿 AC4） | T-07 |
| 2026-09-12 | 新增 §1.2 磁盘一致性方向（落盘/删除两个方向都只留孤儿 + 三类孤儿处置表 + 两处边界说明）与 §1.3 容量口径（`bytes_claimed` vs `bytes_on_disk`，AC6 必须用后者） | T-08 |
