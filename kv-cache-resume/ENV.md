# ENV · 实验环境记录

> 由 [T-01](../tickets/T-01-runtime-and-model.md) 主责维护；其他票按需追加章节。
> 目的是**让每一次实验都可复现**：工具版本、模型哈希、硬件基线都要能被后来人核对。

---

## 1. Python 环境（T-05 补充）

**决策：复用 LexAgent 仓库根 `.venv`，不在本目录新建虚拟环境。**

| 项 | 值 |
| :--- | :--- |
| 解释器 | `C:\Code\py_code\PythonProject\LexAgent\.venv\Scripts\python.exe` |
| 版本 | Python 3.13.5（Anaconda 打包构建） |
| 关键依赖 | `httpx 0.28.1`、`pytest 9.1.1`、`ruff`（均已在根 dev 依赖组内） |

理由：

1. 本目录需要的三个依赖（`httpx` / `pytest` / `ruff`）**根环境已经齐全**，新建 venv 只会多一份几百 MB 的重复安装；
2. 依 T-05 备注，**不在根 `pyproject.toml` 加任何依赖** —— 本目录自带 `pyproject.toml` 只声明依赖清单，不改动根配置；
3. 全程离线 mock，不装 GPU/推理栈，环境负担为零。

本目录 `pyproject.toml` 定位是**声明性质的（依赖清单 + 工具配置）**，不作为强制安装入口；
若将来 T-06 需要 `httpx` 之外的库，再评估是否拆出独立 venv。

### 怎么跑

```bash
# 从仓库根跑本目录的全部离线单测（推荐，CI 友好）
.venv/Scripts/python.exe -m pytest kv-cache-resume/tests -q

# 从本目录跑（pyproject.toml 里的 testpaths 生效）
cd kv-cache-resume && ../.venv/Scripts/python.exe -m pytest -q

# lint 门禁（与 LexAgent 同款口径）
.venv/Scripts/ruff.exe check kv-cache-resume/ && .venv/Scripts/ruff.exe format --check kv-cache-resume/
```

> ⚠️ **不要**用裸 `pip` / `python` —— 本机 `python` 指向基础解释器、`pip` 指向 `envs/default` 虚拟环境，两者不是同一个（跨项目踩过的坑）。统一走上面 `.venv/Scripts/` 的显式路径。

---

## 2. llama.cpp 运行时时（T-01 · 待填）

> 待 [T-01](../tickets/T-01-runtime-and-model.md) 完成：工具路径、`llama-server --version` 的 build 号与后端信息。

| 项 | 值 |
| :--- | :--- |
| 版本 / build 号 | _待 T-01_ |
| 后端 | _待 T-01（必须是 CUDA，不是 CPU-only build）_ |
| 解压路径（仓库外） | `C:\Tools\llama.cpp\` |
| 下载来源 | `ggml-org/llama.cpp` release `b10809`：`llama-b10809-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip` |

---

## 3. 模型（T-01 · 待填）

| 项 | 值 |
| :--- | :--- |
| 模型 | `qwen2.5:3b`（Q2 决策：3B 先行，SPEC §9.2） |
| 路径 | _待 T-01_ |
| sha256 | _待 T-01_ |
| 量化档 | _待 T-01_ |

> Q3 的试探路径：Ollama 的 model blob 本身就是 GGUF，可尝试直接 `llama-server -m <blob 路径>` 省一次下载。
> 新版 Ollama 的 blob 布局可能变动，试探失败就直下 GGUF，**别卡在这一步**。

---

## 4. 硬件基线（T-01 核对 · 部分已实测）

| 项 | 值 | 来源 |
| :--- | :--- | :--- |
| GPU | NVIDIA GeForce RTX 4050 Laptop GPU | `nvidia-smi` 2026-09-12 |
| 显存总量 | 6141 MiB | 同上（与 SPEC §9.1 一致） |
| 显存占用（空闲基线） | 1081 MiB | 同上（SPEC §9.1 记的是 350 MiB，**本机当前更高，T-04 记账时以当时的实测为准**） |
| 驱动 | 592.82 | 同上 |

---

## 5. 变更记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-09-12 | T-05 补充 §1 Python 环境决策与运行方式；建立文件骨架，T-01 各节留待填充 |
