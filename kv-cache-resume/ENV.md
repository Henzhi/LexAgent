# ENV · 实验环境记录

> 由 [T-01](../tickets/T-01-runtime-and-model.md) 主责维护；其他票按需追加章节。
> 目的是**让每一次实验都可复现**：工具版本、模型哈希、硬件基线都要能被后来人核对。
>
> 自检脚本：[`scripts/00_env_check.ps1`](./scripts/00_env_check.ps1) —— 一次跑完输出全 ✅ 即为环境就绪。

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
3. E1 全程离线 mock，不装 GPU/推理栈，环境负担为零。

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

## 2. llama.cpp 运行时（T-01 ✅ 2026-09-12）

| 项 | 值 |
| :--- | :--- |
| 版本 | `0.4.0-dev` |
| **build** | **10809** |
| commit | `5266f24da` |
| 编译器 | Clang 20.1.8 for Windows x86_64 |
| 后端 | **CUDA（非 CPU-only）** —— `ggml-cuda.dll` 存在，`--list-devices` 报 `CUDA0` |
| 解压路径（仓库外） | `C:\Tools\llama.cpp\bin` |
| 可执行文件 | `llama-server.exe` / `llama-cli.exe` / `llama-bench.exe` / `llama-quantize.exe` |
| 下载来源 | `ggml-org/llama.cpp` release **`b10809`** |
| 资产 | `llama-b10809-bin-win-cuda-12.4-x64.zip`（253,938,543 B）+ `cudart-llama-bin-win-cuda-12.4-x64.zip`（391,443,627 B） |

> **为什么选 CUDA 12.4 而不是 13.3**：本机驱动 592.82，12.4 运行时是稳妥匹配档。
> R6 提醒上游 API 未承诺稳定 —— build 号记在这里，将来 slot API 行为变了可以回溯到是哪个版本变的。

### 2.1 为什么日志里会出现「所在位置 …」

用 PowerShell 5.1 抓原生命令输出时，`& exe --version 2>&1 | Out-String` 会把 stderr 行包成
`ErrorRecord`，渲染时插进「所在位置 …」这类定位信息。`00_env_check.ps1` 里的
`Invoke-NativeCapture` 用临时文件重定向绕开这一点（首版踩过）。

---

## 3. 模型（T-01 ✅ 2026-09-12）

| 项 | 值 |
| :--- | :--- |
| 模型 | `qwen2.5:3b-instruct`（Q2 决策：**3B 先行**，SPEC §9.2） |
| 量化档 | **Q4_K - Medium**（`llama-cli` 报 `ftype : Q4_K - Medium`） |
| 文件大小 | `1,929,903,008` bytes（1.80 GiB） |
| **sha256** | `5ee4f07cdb9beadbbb293e85803c569b01bd37ed059d2715faa7bb405f31caa6` |
| 工作路径 | `C:\Tools\llama.cpp\models\qwen2.5-3b-instruct-q4_k_m.gguf` |
| 来源 | **Q3 试探成功**：直接复用 Ollama 的 model blob 当 GGUF |
| 原始 blob | `C:\Users\MaHuhu\.ollama\models\blobs\sha256-5ee4f07cdb9beadbbb293e85803c569b01bd37ed059d2715faa7bb405f31caa6` |
| 取用方式 | **NTFS 硬链接**（同盘、零额外占用；链接计数 2）。Ollama 若回收 blob，硬链接仍独立有效 |

**Q3 结论：Ollama 的 blob 就是裸 GGUF**，无需重新下载。核验三点：

1. Ollama manifest `qwen2.5/3b` 的 `application/vnd.ollama.image.model` 层 digest = `sha256:5ee4f07cdb…`，与 blob 文件名一致；
2. 文件 sha256 实算 = `5ee4f07cdb9beadbbb293e85803c569b01bd37ed059d2715faa7bb405f31caa6`，**与 digest 相符**；
3. 文件头 4 字节 = `47 47 55 46`（ASCII `GGUF`）。

> 复算命令：`sha256sum <blob>`；硬链接复建：`New-Item -ItemType HardLink -Path <目标> -Target <blob>`。

---

## 4. 硬件基线（T-01 核对 ✅）

| 项 | 实测值 | 时间 |
| :--- | :--- | :--- |
| GPU | NVIDIA GeForce RTX 4050 Laptop GPU | 2026-09-12 |
| 显存总量 | **6141 MiB** | 2026-09-12 |
| 显存占用（空闲基线） | 1051~1099 MiB（多次采样） | 2026-09-12 |
| llama.cpp 可用显存 | `CUDA0 … 6140 MiB, 5072 MiB free` | 2026-09-12 |
| 驱动 | 592.82 | 2026-09-12 |

> ⚠️ **与 SPEC §9.1 的差异**：SPEC 记的是「占用 350 MiB / 余 5.7 GB」，实测空闲占用已达 ~1.05 GB
> （Ollama 的常驻服务占着）。**T-04 记账时以当时的实测为准**，不要照抄 SPEC 的旧数字。
> 这也解释了为什么 6 GB 卡上「给 KV 留多少」必须实测 —— 账面可用显存比型号标称少。
>
> 实测生成速率参考（生成冒烟，Q4_K_M，`-ngl 99`）：**31.7 ~ 37.1 t/s**（多次运行波动）。

---

## 5. 沙箱 / Windows 编码坑（跨票通用，务必遵守）

| 坑 | 现象 | 正确做法 |
| :--- | :--- | :--- |
| **`.ps1` 编码** | 无 BOM 的 UTF-8 被 PS 5.1 按 ANSI 解析，中文与 ✅ 全乱码 | 以 **UTF-8 with BOM** 保存 |
| **双 BOM** | 对已有 BOM 的文件执行 `read(utf-8)` + `write(utf-8-sig)` 会叠成双 BOM，PS 报 `意外的属性 CmdletBinding` | 重编码前**先剥离全部 BOM**（`decode('utf-8-sig').lstrip('\ufeff')`） |
| **`Test-Path $x -and $y`** | PS 把 `-and` 当成 `Test-Path` 的参数 → `NamedParameterNotFound`，且**后续检查被静默跳过**，出现「假全绿」 | 必须写 `(Test-Path $x) -and $y` |
| **脚本内 `exit`** | 用 `&` 在**同一会话**内调用时，`exit` 会终止整个会话，导致输出被截断 | 抓输出时用子进程 `powershell -File …`，或给脚本加 `-ReportPath` 落盘 |

> 第 3 条是本次最危险的一个：它让自检脚本在崩掉一节的情况下仍然报「全部检查通过」。
> 教训 —— **自检脚本必须能被自己证伪**，任何一节异常都要计入失败数。

---

## 6. 变更记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-09-12 | T-05 补充 §1 Python 环境决策与运行方式 |
| 2026-09-12 | T-01 填齐 §2 运行时 / §3 模型 / §4 硬件实测；新增 §5 编码坑清单 |
