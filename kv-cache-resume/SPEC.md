# SPEC — 本地 KV Cache 落盘与跨进程续生成

| 项 | 值 |
| :--- | :--- |
| 状态 | **待评审**（尚未动工） |
| 版本 | v0.1 |
| 日期 | 2026-09-11 |
| 位置 | **`LexAgent/kv-cache-resume/`**（LexAgent 仓库内的独立子目录；2026-09-11 由独立仓库迁入） |
| 定位 | **独立 spike**：不进入 LexAgent 主链路代码路径，不改动 `src/` 任何文件 |
| 前置调研 | LexAgent 仓库 `.workbuddy/memory/2026-09-11.md`（含联网事实核查记录） |

---

## 1. 背景

### 1.1 问题来源

LexAgent（法律 RAG 自主 Agent）的 LLM 层是双后端：

| 后端 | 前缀缓存能力 |
| :--- | :--- |
| **DeepSeek（主）** | ✅ 厂商提供自动前缀缓存（命中小米 ¥0.02/M、未命中 ¥1/M、输出 ¥2/M，**50 倍价差**）；但**进程/会话之外不可控**，服务端状态对客户端不透明，无法跨重启保证复用 |
| **Ollama（降级）** | ❌ **零 KV 复用**。项目走 LangChain `ChatOllama`（`/api/chat`），既无跨请求复用，也无跨进程持久化 |

而 LexAgent 的 ReAct 编排**一次复杂查询要发起 18~20 次 LLM 调用**，其中每次调用的 prompt 前缀（system prompt + 工具 schema + 累积历史）**高度重叠**。云端路径已经吃到自动前缀缓存的红利，**本地降级路径完全裸露**。

### 1.2 第二条动因：断线重连的语义天花板

LexAgent 已实现 SSE 断线续流（决策 D-M3-12）：`StreamEventLog` 把每个事件带 seq 写入 Redis List，重连带 `after_seq` 用游标重放。

但它**补的是「已经产生的事件」，不是「让模型接着往下写」**。当 worker 因进程重启 / 主动取消 / 孤儿超时（30s 未认领）真正停止后，重连用户只能拿到半截答案。

**若 KV 可跨进程恢复，这一层可以闭合。**

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 可度量产出 |
| :--- | :--- | :--- |
| G1 | 验证 llama.cpp slot save/restore 能否实现**跨进程** KV 恢复并**逐字一致**续生成 | 逐字比对报告（token 级 diff） |
| G2 | 量化收益 | `restore_ms` vs `cold_prefill_ms`；`bytes/token` 拟合曲线 |
| G3 | 沉淀可复用的**缓存策略层**（何时 save / 用什么 key / 何时 restore / 何时淘汰） | `strategy.py` 设计 + 单测 |
| G4 | 用 LexAgent F15 的口径度量效果（cache hit/miss） | 与 F15 埋点字段对齐的遥测输出 |

### 2.2 非目标

- ❌ **不改 LexAgent 主链路**——本子目录与 `src/` 完全隔离，通过后续 Phase 2 的适配层对接
- ❌ 不做多用户并发 / 生产级可用性（spike 定位）
- ❌ 不做量化方案对比、不做模型能力评测
- ❌ 不替代云端 API 主路径

---

## 3. 技术选型

### 3.1 选定：**llama.cpp 的 slot save/restore**

| 能力 | 说明 |
| :--- | :--- |
| 服务端开关 | `llama-server --slot-save-path <DIR>`；**不设该 flag 时 save/restore 返回 501** |
| 保存 | `POST /slots/<id>?action=save`，body `{"filename":"x.bin"}` |
| 恢复 | `POST /slots/<id>?action=restore`，body `{"filename":"x.bin"}` |
| 内存缓存 | `--cache-ram N`（默认开，**重启即丢**） |
| 插件化 API | `llama_state_seq_get_data` / `llama_state_seq_set_data`（序列级 KV 拷贝/恢复，附 RNG/logits 状态） |

**决定性理由**：官方把「自动落盘」这个 feature request（**#17107**）**关闭为 not planned**，理由是设计取舍——

> **server 只提供机制（save/restore），policy（何时存、用什么 key、何时恢复）留给 client。**

这意味着本项目的核心工作量落在**策略层**，而不是引擎改造。而策略层的模式与 LexAgent 已有的 `StreamEventLog` **完全同构**（见 §4.1）——不是从零学新东西，是把已有纪律迁移到新介质。

**平台优势**：llama.cpp 原生支持 Windows + CUDA，与本机 RTX 4050 Laptop 直接匹配。

### 3.2 已否决：vLLM + LMCache

| 否决理由 | 依据 |
| :--- | :--- |
| **平台不匹配** | vLLM 需 Linux（WSL2 / 容器），本机为 Windows；llama.cpp 原生 Windows + CUDA |
| **没有按请求恢复的 API** | vLLM 的 `pause_generation` / `resume_generation` 是**全局**操作，为 Async RL 权重同步设计（`mode="keep"` 冻结在途请求），**不是「按 request_id 恢复某个客户端请求」** |
| **新请求不自动续用** | OpenAI 兼容 API 的新请求**不会**自动复用之前请求的 KV cache；复用仅发生在引擎仍持有该序列 / 前缀缓存精确命中 / 通过外部 KV 存储恢复三种情况 |
| **引擎自身不保证保住** | vLLM 抢占（显存不足）会**直接丢弃 KV blocks**，恢复时须从 prompt 开头重算全部 KV |
| **引入重量** | LMCache 需 GPU/CPU/SSD 三层 KV 外挂存储 + connector 配置，成本远超 spike 定位 |

> vLLM 路径保留为**未来备选**：若日后部署形态迁到 Linux + 独立 GPU 机器，可重新评估 `--enable-prefix-caching` + LMCache。

### 3.3 已否决：Ollama

Ollama 底层即 llama.cpp，但**不暴露 slot save/restore**（`/api/chat` 与 `/api/generate` 均无对应能力）。这是本 spike 必须脱离 `ChatOllama` 的直接原因。

---

## 4. 架构设计

### 4.1 与 LexAgent 现网机制的同构关系

这是本设计的核心认知：**我们不发明新模式，而是复用 LexAgent 已验证的「单一真相源 + 单调游标 + 失效策略」模式，只更换介质。**

| 维度 | LexAgent D-M3-12（已上线） | 本项目 |
| :--- | :--- | :--- |
| 单一真相源 | Redis List `lexagent:stream:{id}:events` | 磁盘 `slotcache_{key}.bin` |
| 定位键 | `request_id` + 单调 `seq` | `key = hash(完整 token 序列)` |
| 写入时机 | worker 先落日志再投递在线队列 | 生成结束后 save slot |
| 恢复动作 | `read_after(last_seq)` 重放事件 | `action=restore` 装回 KV |
| 失效策略 | TTL 600s | TTL + LRU + 目录容量上限 |
| 失败语义 | 日志故障只告警，不阻断主链路 | 落盘/恢复失败回退冷 prefill，不阻断请求 |
| 单写者纪律 | 仅 worker 线程写，故 seq 用 `LLEN+1` 无需锁 | 仅策略层写，同 key 串行化 |

### 4.2 组件与数据流

```
                    ┌──────────────────────────────┐
   请求(token 序列) ─►│  Policy Layer (本项目核心)     │
                    │  1. key = hash(tokens)        │
                    │  2. 查索引 → 命中?             │
                    └───┬──────────────────────┬───┘
                        │ 命中                  │ 未命中 / 恢复失败
                ┌───────▼────────┐      ┌───────▼─────────┐
                │ action=restore │      │ 冷 prefill      │
                │ 装回 KV 到 slot │      │ （正常推理）      │
                └───────┬────────┘      └───────┬─────────┘
                        └────────┬──────────────┘
                                 ▼
                        ┌─────────────────┐
                        │  decode 继续生成  │
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐      ┌──────────────┐
                        │ 生成结束 → save  │─────►│ 磁盘 KV 文件   │
                        └─────────────────┘      │ + 索引元数据   │
                                                  └──────────────┘
```

三块组件：

1. **Engine Adapter** — 封装 `llama-server` 的 `/slots` HTTP 调用（save / restore / 健康检查），把 501 等错误显式暴露。
2. **Policy Layer** — 前缀键计算、命中判断、淘汰决策、失败降级。**本项目的实质交付物。**
3. **Telemetry** — 输出 `restore_hit` / `restore_miss` / `saved_bytes` / `restore_ms` 事件，字段口径对齐 LexAgent F15。

### 4.3 prefix key 定义

```
key = sha256( model_id  ‖  quant  ‖  token_ids(完整序列) )[0:16]
```

**必须逐 token 一致才命中。** 下列任一变化都会击穿缓存，退化为全量 prefill：

- chat template 的空白/换行差异
- **工具 schema 的 JSON key 顺序**（直接相关：LexAgent 的 schema 由 pydantic 从类型注解推导）
- system prompt 中的时间戳、随机 ID、动态内容
- 温度/采样参数（不改变 KV，但应纳入 key 以避免误用）

配套索引元数据（`slotcache_{key}.meta.json`）：

```json
{
  "key": "a3f5b2c9d8e1f7a6",
  "model_id": "qwen2.5-3b-instruct",
  "quant": "Q4_K_M",
  "n_tokens": 4128,
  "bytes": 219_430_912,
  "created_at": "2026-09-11T21:00:00+08:00",
  "last_used_at": "2026-09-11T21:08:00+08:00",
  "hits": 3
}
```

### 4.4 存储布局

```
<slot-save-path>/
├── slotcache_{key}.bin           # KV 张量本体
├── slotcache_{key}.meta.json     # 索引元数据（键/模型/token 数/字节数/LRU 时间戳/命中次数）
└── index.json                    # 全局索引：key → meta 路径；容量统计
```

---

## 5. 需求（EARS）

### Ubiquitous

- **REQ-U1** The system shall 使用 `hash(模型标识 + 量化 + 完整 token 序列)` 作为 KV 缓存的唯一键。
- **REQ-U2** The system shall 在任何 restore 之前校验模型标识与量化版本；不匹配则丢弃该缓存并走冷 prefill。
- **REQ-U3** The system shall 保持所有 KV 缓存文件的写入为**单写者**语义（同一 key 串行），避免并发写坏文件。

### Event-driven

- **REQ-E1** When 一次生成正常结束或达到 `max_tokens`，the system shall 将当前 slot 的 KV 落盘并写入索引元数据。
- **REQ-E2** When 收到与已有缓存前缀键相同的请求，the system shall 优先尝试 restore 该 KV，而非重新 prefill。
- **REQ-E3** When slot save 或 restore 返回 HTTP 501（未配置 `--slot-save-path`），the system shall 明确报错并降级为「无缓存模式」，不得静默失效。
- **REQ-E4** When 一次 restore 命中，the system shall 更新该缓存的 `last_used_at` 与 `hits`。

### State-driven

- **REQ-S1** While KV 缓存目录占用超过配置上限（`KV_MAX_BYTES`），the system shall 按 LRU 淘汰至低于上限。
- **REQ-S2** While 缓存条目数超过 `KV_MAX_ENTRIES`，the system shall 按 LRU 淘汰至低于上限。

### Unwanted

- **REQ-W1** If restore 失败、超时或返回非 2xx，then the system shall 回退为冷 prefill 并照常完成请求，**不得中断请求**。
- **REQ-W2** If 前缀 token 序列校验不一致，then the system shall 放弃该缓存（宁可重算，也不用错 KV 产出错误结果）。
- **REQ-W3** If 落盘失败，then the system shall 仅记录告警，不阻断生成。
- **REQ-W4** If 磁盘可用空间低于 `KV_MIN_FREE_BYTES`，then the system shall 停止落盘、告警，并在空间恢复后自动恢复落盘。

### Optional

- **REQ-O1** Where 启用逐字比对模式（`VERIFY_EXACT=true`），the system shall 在 save→restart→restore→续生成后，与不中断的完整轨迹做 token 级 diff 并输出结论。
- **REQ-O2** Where 启用遥测，the system shall 输出 `restore_hit` / `restore_miss` / `saved_bytes` / `restore_ms` / `cold_prefill_ms`，字段口径与 LexAgent F15 的 cache hit/miss 对齐。
- **REQ-O3** Where 显存不足以容纳目标上下文，the system shall 允许通过 `KV_QUANT`（f16/q8/q4）下调 KV 精度，并在元数据中记录该参数。

---

## 6. 验收标准

| 编号 | 判据 | 目标值 | 验证方式 |
| :--- | :--- | :--- | :--- |
| **AC1** | 逐字一致：save → **杀进程** → 重启 → restore → 续生成，与不中断轨迹 token 级完全相同（temp=0） | 100% 一致 | `VERIFY_EXACT` 比对脚本 |
| **AC2** | restore 耗时 vs 冷 prefill 耗时（4K token 量级） | **≥ 5× 提速** | 各自 3 次取中位数 |
| **AC3** | KV 文件大小对 token 数线性可预测 | 拟合 R² > 0.95，预测误差 < 20% | 多量级采样拟合 |
| **AC4** | 同前缀重发命中率 | ≥ 99%（100 次重发） | 遥测统计 |
| **AC5** | 失败降级：restore 被强制失败时 | 请求仍成功完成（回退冷 prefill） | 注入 501/超时 |
| **AC6** | 容量上限生效 | 目录占用始终 ≤ `KV_MAX_BYTES` | 写入超量数据后检查 |
| **AC7** | 前缀扰动检测：schema key 顺序 / 空白 / 时间戳变动 | 正确判定为 miss（不使用错 KV） | 构造 3 组扰动样本 |

---

## 7. 实验计划

### Phase 0 · 机制验证（半天）

1. 安装 llama.cpp 的 Windows CUDA release
2. 起服务：`llama-server -m <model> --slot-save-path ./kv -ngl 99 -c <ctx>`
3. 跑验证链：`生成 → save → 杀进程 → 重启 → restore → 续生成 → 逐字比对`
4. **产出 AC1 / AC2 / AC3 三个数**

> 模型获取：Ollama 的 model blob 本身就是 GGUF，可尝试直接 `llama-server -m <blob 路径>` 省一次下载；不可用再单独下 GGUF。
> **建议先用 `qwen2.5:3b`（1.93GB）把机制跑通**，不要一上来用 7B。

### Phase 1 · 策略层（1~2 天）

- `strategy.py`：prefix key 计算、命中判断、LRU 淘汰、失败降级
- 单测覆盖 REQ-U2 / REQ-S1 / REQ-S2 / REQ-W1~W4（**全部离线 mock，不依赖真实 server**）
- **产出 AC4 / AC5 / AC6 / AC7**

### Phase 2 · 接回 LexAgent（需架构决策，暂不启动）

⚠️ **已知冲突**：LexAgent LLM 层走 LangChain `ChatOllama`（`/api/chat`），**拿不到 `/slots` API**。两条路径需先决策：

- (a) 自包一个 LangChain `BaseChatModel` 直连 `llama-server`（保留生态互操作）
- (b) 把 `llama-server` 作为**新的降级后端**并列于 Ollama

⚠️ **铁律**：新后端**必须挂 `callbacks=budget_callbacks()`**，否则 F14 预算静默漏计（LexAgent 有 `test_callback_mounted_on_real_backends` 守这条）。

⚠️ 必须带**开关 + 一键回滚**，且默认关闭。

### Phase 3 · 与 ReAct 结合（真正的收益点）

- 目标：让 ReAct 循环 18~20 次调用之间复用 system + 工具 schema 前缀
- 度量：对比开关前后的 `cache_miss_tokens` 与端到端 prefill 总耗时
- 产出：一份「本地 KV 复用对 ReAct 成本的影响」报告

---

## 8. 风险与缓解

| 编号 | 风险 | 影响 | 缓解 |
| :--- | :--- | :--- | :--- |
| **R1** | 前缀不一致导致**不命中**（schema key 顺序 / 空白 / 时间戳） | 收益归零，退化为全量 prefill | AC7 专项验证；策略层对不稳定字段做归一化或在 key 中显式排除 |
| **R2** | **KV 文件膨胀**（实测 4K token ≈ 219MB@27B 模型，线性增长） | 吃满磁盘 | REQ-S1/S2 容量上限 + LRU；REQ-W4 磁盘水位保护 |
| **R3** | **显存不足**：6GB 下 7B Q4 仅余约 1.4GB 给 KV | OOM / 上下文受限 | 先用 3B 验证；REQ-O3 支持 KV 量化；必要时降 `-c` |
| **R4** | 与 LexAgent 架构冲突（`ChatOllama` 无 slot API） | Phase 2 阻塞 | 已在 §7 Phase 2 列出两条候选路径，**决策前不动手** |
| **R5** | llama.cpp 官方已知坑 | 保存不完整/阻塞 | vision(mmproj) 模型会**阻塞 slot-save**（本项目无关）；SWA 类模型需 `--swa-full` 否则保存不完整 |
| **R6** | 上游 API 演进（llama.cpp 迭代快，slot API 未承诺稳定） | 升级即坏 | 元数据记录版本；Engine Adapter 层做能力探测；REQ-E3 的 501 显式报错 |
| **R7** | **投入产出比**：LexAgent 线上主路径已吃云端前缀缓存，本 spike 的直接线上收益有限 | 项目价值被质疑 | 立项理由是 G3（策略层能力沉淀）+ G4（跨进程持久化为云端所不能），**明确不承诺线上收益** |

### 8.1 立项价值的诚实陈述

本项目的价值**不在**"给 LexAgent 省多少钱"——主路径已由 DeepSeek 前缀缓存覆盖。价值在三处：

1. **本地降级路径**从「零复用」变为「有复用 + 可跨进程」，改善降级时的体验
2. **跨进程持久化**是云端 API 结构上给不了的（服务端状态对客户端不透明）
3. **能力沉淀**：KV 缓存策略层是大模型工程的核心话题，且本项目已有 F15 埋点可量化，是稀缺的**可度量**实践

---

## 9. 硬件与显存预算

### 9.1 本机实测

| 项 | 值 |
| :--- | :--- |
| GPU | NVIDIA GeForce RTX 4050 Laptop GPU |
| 显存 | **6141 MiB**（当前占用 350 MiB，利用率 0%） |
| 驱动 | 592.82 |
| 已装 | Ollama（`qwen2.5:3b` 1.93GB / `qwen2.5:7b` 4.68GB / `bge-m3` 1.16GB / `nomic-embed-text`） |
| 缺 | `llama-server` / `llama-cli` / `llama-cpp-python` |

### 9.2 KV 开销估算（Qwen2.5-7B：28 层 / 4 KV heads / head_dim 128）

单 token KV 大小 = `2 × n_layers × n_kv_heads × head_dim × bytes`

| KV 精度 | 单 token | 1.4GB 可用显存下容量 |
| :--- | ---: | ---: |
| f16（2B） | ≈ 57 KB | ≈ 2.4 万 token |
| q8（1B） | ≈ 28.6 KB | ≈ 4.9 万 token |

> 注意：开启 embedding / reranker（bge-m3、bge-reranker-v2-m3）会额外占用显存，实测算总账。
> **实验阶段建议 `qwen2.5:3b`**，留足余量先把机制跑通。

---

## 10. 附录 · 事实核查记录

以下为本 spec 立论依赖的外部事实，均已联网核实（2026-09-11）：

| # | 事实 | 来源 |
| :--- | :--- | :--- |
| 1 | `llama-server --slot-save-path` 暴露 `POST /slots/<id>?action=save\|restore`；不设该 flag 返回 **501** | llama.cpp 讨论区 #18244、第三方实践文 |
| 2 | 官方将「自动落盘」feature request **#17107 关闭为 not planned** —— 机制给 server、policy 给 client | 同上 |
| 3 | 实测参考（27B 模型、4K token slot）：save **211ms / 219MB**，restore **87ms**，对比冷 prefill 十几秒 → **7× 提速**（长上下文估 25~40×）；文件线性增长（50K ≈ 2.7GB） | 同上 |
| 4 | vision(mmproj) 模型会**阻塞 slot-save**；SWA 类模型需 `--swa-full` | 同上 |
| 5 | vLLM 的 `pause_generation` / `resume_generation` 是**全局**的，为 Async RL 权重同步设计，非按请求恢复 | vLLM 官方文档（Async RL） |
| 6 | OpenAI 兼容 API 的**新请求不会自动续用**之前请求的 KV cache | KV Cache Engineering 综述 |
| 7 | vLLM 抢占会**直接丢弃 KV blocks**，恢复时从 prompt 开头重算 | KV Cache 磁盘卸载研究 |
| 8 | llama.cpp 提供 `llama_state_seq_get_data` / `set_data` 序列级 KV 拷贝与恢复 | llama.cpp `save-load-state` 示例 |
| 9 | LexAgent 现网机制（事件日志 + seq 游标、被动断线 worker 继续跑、30s 孤儿宽限） | LexAgent `AGENTS.md` 约定 12、`src/observability/stream_log.py` |

---

## 11. 待决策项

| 编号 | 待决策 | 影响 | 建议 |
| :--- | :--- | :--- | :--- |
| Q1 | 是否下载 llama.cpp Windows CUDA release（约 200~400MB） | Phase 0 前置 | 建议下载，无替代路径 |
| Q2 | 实验用模型（3B 跑通 vs 直接 7B） | 显存余量 | 建议 **3B 先行** |
| Q3 | 是否先用 Ollama blob 当 GGUF 省下载 | 节省一次下载 | 先试，失败再下 |
| Q4 | Phase 2 走 (a) 自包 BaseChatModel 还是 (b) 并列新后端 | 架构改动面 | Phase 1 结束后再定 |
| Q5 | 仓库是否建为公开 repo | 作品集价值 | 建议公开（与 LexAgent 叙事互补） |
