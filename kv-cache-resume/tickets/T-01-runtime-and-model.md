# T-01 · llama.cpp 运行时就绪与模型取用

| 项 | 值 |
| :--- | :--- |
| 状态 | ✅ 完成（2026-09-12） |
| Epic | E0 · 机制验证（SPEC Phase 0） |
| 阻塞于 | 无 —— 可立即开始 |
| 阻塞 | T-02 |
| SPEC 依据 | §11 Q1/Q2/Q3、§9.1、R3 |
| 预估 | 1~2 h |

## 要做出什么

本机能跑起 `llama-server`，并有一个实验用 GGUF 模型落地。工具版本、模型来源与哈希记录在案，**让后面每一次实验都可复现**。

后面所有 E0 的票都建立在这一步上，任何一项缺失都会让 AC1~AC3 无法验证。

## 范围

**做**

- 下载 llama.cpp 的 **Windows CUDA release**（约 200~400 MB），解压到**仓库外**的工具目录（不入库，`.gitignore` 只管仓库内）
- 记录 `llama-server --version` 输出的 build 号与后端信息 —— R6 要求元数据可追溯引擎版本
- 获取实验模型，优先级：
  1. 直接复用 Ollama 已有模型的 blob 当 GGUF（`qwen2.5:3b`，1.93 GB）—— 省一次下载（Q3）
  2. 不可用则单独下载 `qwen2.5:3b` 的 Q4_K_M GGUF
- 核对硬件基线：RTX 4050 Laptop 的可用显存实测值（SPEC §9.1 记的是 6141 MiB / 当前占用 350 MiB）
- 记录模型文件的 sha256（后面 AC1/AC3 的复现依据）

**不做**

- 不写任何策略层代码（那是 E1）
- 不改动 `src/` 任何文件
- 不下载 7B 模型（Q2 建议 3B 先行，SPEC §9.2 已说明理由）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/ENV.md` | 工具路径 / build 号 / 模型路径与 sha256 / 显存实测 / 实验用 Python 环境选择 |
| `kv-cache-resume/scripts/00_env_check.ps1` | 环境自检：可执行文件存在、能打印版本、模型可加载、显存查询 |

## 验收清单

- [x] `llama-server --version` 有输出，且含 CUDA 后端信息（不是 CPU-only build）
- [x] 模型文件落地，`llama-cli -m <model> -p "1+1=" -n 8` 能正常吐出 token
- [x] `ENV.md` 记录了 build 号、模型 sha256、显存实测值
- [x] `00_env_check.ps1` 一次跑通，输出全是 ✅

## 实施记录（2026-09-12）

| 项 | 结论 |
| :--- | :--- |
| llama.cpp | **build 10809** / `0.4.0-dev` / commit `5266f24da`，Clang 20.1.8，Windows x86_64 |
| 资产选择 | CUDA **12.4**（非 13.3）：本机驱动 592.82，12.4 是稳妥匹配档 |
| 安装位置 | `C:\Tools\llama.cpp\bin`（仓库外，不入库）；**不需要管理员权限** |
| 模型 | qwen2.5:3b Q4_K_M，1.80 GiB，sha256 `5ee4f07cdb…` |
| **Q3 答案** | **Ollama blob 就是裸 GGUF，可直接用**（manifest digest == 实算 sha256，魔数 `GGUF`），省掉一次 1.9GB 下载 |
| 取用方式 | NTFS **硬链接**到 `models\qwen2.5-3b-instruct-q4_k_m.gguf`，零额外占用 |
| 显存 | 总量 6141 MiB；空闲占用实测 **~1.05 GB**（SPEC §9.1 记的 350 MiB 已过时，Ollama 常驻占着） |
| 生成速率 | 31.7 ~ 37.1 t/s（Q4_K_M，`-ngl 99`） |

### 三个与票面写法的偏差（实测纠正）

1. **`llama-cli` 的调用方式**：票面写的 `llama-cli -m <model> -p "1+1=" -n 8` 在 build 10809 下会进入
   **会话模式并等待输入**（不会自己退出）。必须加 `-st`（`--single-turn`）。
   注意 `--no-conversation` 在这个版本**不存在**（会报 `invalid argument`）。
2. **自检脚本验收**：票面要求「输出全是 ✅」。首版脚本因 `Test-Path $x -and $y` 的 PS 解析陷阱
   崩掉一整节却仍报「全部检查通过」—— 属于**假全绿**，已修（详见 `ENV.md` §5）。
3. **生成断言**：不能断言「输出恰好是 `1 + 1 = 2`」。同一命令不同次运行会给出
   `1 + 1 = 2` / `1 + 1 equals 2.` 等不同文本（采样波动）。已改为断言「产出了 token」
   （`Generation: N t/s`）并额外加 `--temp 0 --seed 42` 降低波动。

> 这三条都是「票面写法 vs 实测行为」不一致，按 `tickets/README.md` 的约定**以实测为准**并回写本记录。

## 备注

- **Ollama blob 未必是裸 GGUF**：新版 Ollama 的 blob 布局可能变动。试探失败就直下 GGUF，**别卡在这一步**。
- 显存只有 6 GB，选 Q4_K_M 而不是更大的量化档；SPEC §9.2 的估算公式（`2 × n_layers × n_kv_heads × head_dim × bytes`）在 T-04 会被实测校验。
- 如果本机最终跑不通 CUDA（驱动/算力不匹配），**在第 1 批就停手并上报**——E1 仍然能全离线做完，但 AC1~AC3 要换机器验证。

## 完成后

更新 [`README.md`](./README.md) 状态表 → 开始 [T-02](./T-02-slot-api-smoke.md)
