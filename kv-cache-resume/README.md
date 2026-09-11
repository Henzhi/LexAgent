# kv-cache-resume

用 llama.cpp 的 **slot save/restore** 把本地 LLM 的 KV cache 落盘，实现**跨进程**的前缀复用与断点续生成。

> **位置**：`LexAgent/kv-cache-resume/` —— LexAgent 仓库内的独立子目录（spike）。
> **分支**：`feat/kv-cache-resume`（开发期；完成后合回 `main`）。
> **状态**：🛠 **已拆票待动工**（2026-09-11）。需求与设计见 [`SPEC.md`](./SPEC.md)，执行单元见 [`tickets/`](./tickets/README.md)。

## 这是什么

LexAgent（法律 RAG Agent）一次复杂查询要发起 18~20 次 LLM 调用，每次 prompt 前缀高度重叠：

- 云端主路径（DeepSeek）已吃到厂商的自动前缀缓存
- **本地降级路径（Ollama）零 KV 复用，且无任何跨进程持久化**

本项目在 llama.cpp 上补上后者：把 KV 张量按前缀键落盘，重启后仍能恢复并**接着生成**。

## 核心认知

llama.cpp 官方把「自动落盘」标为 **not planned**，设计取舍是：

> **server 只提供机制，policy 留给 client。**

所以本项目的实质交付物是一个**策略层**（何时 save / 用什么 key / 何时 restore / 何时淘汰）。它的模式与 LexAgent 已上线的 `StreamEventLog`（单写者 + 单调 seq 游标 + TTL）**完全同构**——只是介质从 token 事件换成了 KV 张量。细节见 [`SPEC.md` §4.1](./SPEC.md)。

## 选型速查

| 方案 | 结论 |
| :--- | :--- |
| **llama.cpp slot save/restore** | ✅ **选定**（Windows 原生 + CUDA，有落盘 API） |
| vLLM + LMCache | ❌ 否决（需 Linux；`pause/resume` 是全局的，非按请求；新请求不自动续用 KV） |
| Ollama | ❌ 否决（底层虽是 llama.cpp，但不暴露 slot API） |

## 目录

```
.
├── README.md      # 本文件
├── SPEC.md        # 规格说明（需求 / 设计 / 验收标准 / 风险）—— 需求的单一真相源
├── tickets/       # 执行单元：SPEC 拆成 14 张小票（依赖图 + REQ/AC 追溯矩阵）
├── ENV.md         # 工具路径 / 版本 / 模型哈希 / 显存实测（T-01 产出）
├── kv_cache/      # 策略层包（T-05 起）
├── scripts/       # 实验与验收脚本（T-01 起）
├── tests/         # 离线单测（T-05 起）
├── reports/       # 实验原始数据（gitignore）
└── .gitignore
```

## 下一步

**先读 [`tickets/README.md`](./tickets/README.md)** —— 那里有依赖图、状态表和推荐执行顺序。

两条线可以并行：

| 线 | 起点 | 环境依赖 |
| :--- | :--- | :--- |
| E0 机制验证（AC1~AC3 三个数） | [T-01](./tickets/T-01-runtime-and-model.md) | 需 GPU + 真实 `llama-server` |
| E1 策略层（**实质交付物**） | [T-05](./tickets/T-05-skeleton-and-config.md) | 离线可做，不需要 GPU |

E0 的前置条件是安装 llama.cpp 的 Windows CUDA release（见 `SPEC.md` §11 Q1）。

## 与 LexAgent 的关系

- **独立 spike**：物理上位于 LexAgent 仓库内（遵循「所有新代码只写在 LexAgent」的项目约定），但**代码路径与 `src/` 完全隔离**，LexAgent 主链路未被修改
- 度量口径对齐 LexAgent 的 F15 埋点（cache hit/miss tokens），便于横向比较
- 若 Phase 2 要接回，需先解决架构冲突（`ChatOllama` 拿不到 `/slots` API），详见 `SPEC.md` §7 Phase 2
