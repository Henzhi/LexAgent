# Phase 0 · AC1 跨进程续生成实测报告（T-03）

| 项 | 值 |
| :--- | :--- |
| 日期 | 2026-09-12 |
| 结论 | **AC1 通过 —— token 级 100% 一致** |
| 引擎 | llama.cpp build 10809（`0.4.0-dev`, commit `5266f24da`） |
| 模型 | `qwen2.5-3b-instruct-q4_k_m.gguf` |
| 验证链脚本 | [`scripts/03_verify_exact.py`](../scripts/03_verify_exact.py) |
| 原始数据 | [`reports/ac1-exact-match.json`](../reports/ac1-exact-match.json) |
| 需求依据 | SPEC G1、REQ-O1、AC1 |

> 本文只写**实测**。与 SPEC 冲突处以本文为准（`tickets/README.md` 的约定）。

---

## 0. 结论速查

| # | 结论 | 影响 |
| :--- | :--- | :--- |
| A1 | **AC1 成立**：「生成 → save → **杀进程** → 重启 → restore → 续生成」的 token 序列与不中断跑完**逐字一致** | 整个 spike 的地基可用 |
| A2 | **K=256 与 K=511 两个分段点都一致**（共同前缀 512/512） | 单点一致不是巧合 |
| A3 | **恢复的 KV 被完整复用**：段 2 的 `cache_n` 与 `save` 的 `n_saved` **完全相等**（268/268、523/523） | restore 不是「假装成功」 |
| A4 | **回灌必须用 token id 数组**，拼文本会 detokenize→retokenize 往返失真 | 见 §4.1 |
| A5 | **`tokens_evaluated` 不是缓存命中指标**，`timings.cache_n` 才是 | 见 §4.2 |
| A6 | **`-c` 不必一致**；真正的约束是**恢复时 `n_saved ≤ n_ctx`** | **T-07 无需把 `n_ctx` 计入键** |
| A7 | KV 体积 **36,883 / 36,882 B/token**（两个 K 值几乎相同） | 交 T-04 做线性拟合（AC3） |

---

## 1. 手法

两条轨迹，同参数同 seed，`temperature=0`：

```
基线：  prompt ──────────────────────────────────────▶ N=512 token     （进程内一次跑完）
分段：  prompt ──▶ K token ──▶ save ──▶ ✄杀进程✄ ──▶ 重启 ──▶ restore ──▶ 续到 N token
                                                      ↑ 段2 用 [prompt_tokens + 段1 tokens] 数组回灌
```

**比对单位是 token id，不是文本**（文本比对会漏掉同形不同 id 的情况）。

### 1.1 参数（全部钉死）

| 参数 | 值 |
| :--- | :--- |
| `model` | `qwen2.5-3b-instruct-q4_k_m.gguf` |
| `ctx` / `ngl` | 4096 / 99 |
| `N`（基线总长） | 512 |
| `K`（分段点） | **256**、**511** |
| `seed` / `temperature` | 42 / **0.0** |
| `cache_prompt` | true |
| prompt | `Write a detailed technical explanation of how a transformer language model works.`（13 token） |

> `temperature=0` + 固定 `seed` 是**硬要求**：KV 里含 RNG 与 logits 状态（SPEC §3.1）。
> 采样参数漂移会让两条轨迹必然分叉 —— 那是脚本问题不是机制问题，但会浪费一整天。

---

## 2. 结果

**基线 sha256（N=512）**

```
abb798774502184244a5ed9d3192605f91fdae7301eb7120ef5df4adb1dc8b5b
```

| K | 基线 | 分段拼接 | 共同前缀 | 分段 sha256 | 判定 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 256 | 512 | 512 | **512 / 512** | `abb7987…c8b5b` | ✅ 一致 |
| 511 | 512 | 512 | **512 / 512** | `abb7987…c8b5b` | ✅ 一致 |

**两条轨迹的 sha256 与基线完全相同**，即逐 token 无一处分歧。

### 2.1 恢复确实生效（A3）

| K | save `n_saved` / `n_written` | restore `n_restored` / `n_read` | 段2 `cache_n` | 差 |
| :--- | :--- | :--- | :--- | :--- |
| 256 | 268 / 9,884,748 B | 268 / 9,884,748 B | **268** | 0 |
| 511 | 523 / 19,289,148 B | 523 / 19,289,148 B | **523** | 0 |

三点值得注意：

- `n_read == n_written`（字节级往返无损）；
- **段 2 的 `cache_n` 等于 `n_saved`** —— 恢复出来的 KV 被完整吃下，不是「restore 返回 200 但实际没用上」；
- `n_saved = prompt(13) + K - 1`。差值 1 是**最后一条生成的 token 尚未进 KV**（它的 logits 已产出，但 KV 要等下一次前向才写入）。所以「续生成」时恰好补算这 1 条，随后从正确位置继续。

### 2.2 幂等性（多轮独立复现）

| 批次 | 参数 | 基线 sha256 |
| :--- | :--- | :--- |
| 自检 ×2 | N=64, K=32/63 | `cb82c7a1d4f5d335…e465f3`（两次相同） |
| 正式 ×3 | N=512, K=256/511 | `abb7987…c8b5b`（三次相同） |

**5 次独立运行、跨进程、跨脚本版本，基线逐字相同** —— 脚本可重复执行，结论稳定。
（进程 pid 每次都不同，见 §3，排除「其实没重启」的可能。）

---

## 3. 进程证据：确实杀了进程（不是假装重启）

AC1 最容易被放水的地方就是「重启」——比如只重开 HTTP 连接。故脚本自己 `Popen` 拉起
`llama-server.exe`，记录 **pid / 启动时刻 / 被杀时刻 / 退出码**，并在 kill 后**断言端口真的释放**。

| gen | pid | 存活 | 退出码 | kill 方式 | 角色 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | 25380 | 18.3 s | 1 | terminate | 基线 |
| 2 | 27144 | 13.7 s | 1 | terminate | K=256 段1 **→ 被杀** |
| 3 | 29244 | 11.9 s | 1 | terminate | K=256 重启（**新 pid**） |
| 4 | 27272 | 18.6 s | 1 | terminate | K=511 段1 **→ 被杀** |
| 5 | 15420 | 6.4 s | 1 | terminate | K=511 重启（**新 pid**） |

- 被杀进程与重启进程 **pid 不同**（27144→29244、27272→15420）→ 确实是新进程。
- 退出码 `1`：Windows 下 `terminate()` 即 `TerminateProcess`，**1 表示「被强杀」而非「崩溃」**。
  这正是本票要的：进程状态一并消失。
- 每轮 kill 后调用 `wait_port_free()`，端口未释放就抛错 —— 不允许「端口还占着」的重启糊弄过去。

---

## 4. 三个踩过的坑（都会让你误判「机制不成立」）

### 4.1 回灌必须用 token id 数组，不能拼文本

最早的做法是把段 1 生成的**文本**拼回 prompt 再发（`prompt = P + content`）。这条路径要经过
detokenize → retokenize 往返，边界处的 token 化不保证还原，于是「明明 restore 了却接不上」。

**正确做法**：`prompt` 传 `[prompt_token_ids...] + [generated_token_ids...]`。
llama.cpp 的 `/completion` 接受 token 数组，序列精确可控。

### 4.2 `tokens_evaluated` 不是缓存命中指标 —— `timings.cache_n` 才是

这是个**代价不小的误判**。`tokens_evaluated` **始终等于 prompt 的 token 总数**，无论命中与否；
按它判断，会得出「缓存复用完全失效、AC1 不可能成立」的结论。

真正区分命中与重算的是 `timings.cache_n`。实测对照（1981-token 长 prompt 连发三次）：

| 次序 | `timings.cache_n` | `prompt_ms` |
| :--- | :--- | :--- |
| 第 1 次 | 1 | 1640.3 |
| 第 2 次 | **1980** | **48.8** |
| 第 3 次 | **1980** | **30.2** |

第二次起快 33 倍 —— 缓存复用一直是好的。**判定机制类问题时要选对观测点。**

### 4.3 「端口可连」不等于「服务就绪」

llama-server **先绑定端口再加载模型**。用 `connect_ex()` 探活会在模型还没读完时就返回「就绪」，
随后的 `/health` 直接失败，报出一个与真实原因无关的错。
必须**轮询 `/health`**，并在失败时把服务日志尾部一起打印出来。

---

## 5. `-c` 不一致会怎样（T-02 移交的前提条件）

T-02 §3 遗留了一条未验证前提：**保存与恢复时 `-c` 是否必须一致**？
这直接影响 T-07 要不要把 `n_ctx` 计入键，故本票顺带实测。

在 `-c 4096` 下生成 256 token 并 save（`n_saved=268`），换不同 `-c` 重启后 restore：

| restore 端 `-c` | 装得下？ | restore | 续生成 |
| :--- | :--- | :--- | :--- |
| **512** | ✅ 268 ≤ 512 | **200**，`n_restored=268` / `n_read=9,884,748` | **与基线逐字一致**（共同前缀 320） |
| **128** | ❌ 268 > 128 | **400** `Unable to restore slot: No available space in KV cache or invalid slot save file` | — |

**结论（A6）**：

1. **`-c` 不必一致。** 512 端比 4096 端小得多，restore 照样成功，续生成照样逐字一致。
2. 真正的约束是 **恢复时 `n_saved ≤ n_ctx`**。装不下时返回 **400**。
3. **T-07 不需要把 `n_ctx` 计入键**。（若当初按「必须一致」实现，会白白让缓存命中率归零。）
4. 这条 400 与 T-02 §6.3 的「文件不存在」是**同一个错误码同一句消息** —— 消息里
   「No available space in KV cache」这半句对本例是**准确**的。两半句分别对应两种成因，
   单看消息确实无法分辨，维持 T-02 的处置：**只当 `MISS(restore_failed)`，不解析 message**。
5. **给 T-09 的可执行规则**：我们自己的 store 知道每条记录的 `n_tokens`，**在发 restore 前先比对
   服务端 `n_ctx`**，装不下就直接判 MISS，省掉一次注定 400 的往返。

---

## 6. 对下游的影响

| 票 | 影响 |
| :--- | :--- |
| **T-04** | KV 体积实测 **36,883 / 36,882 B/token**（两 K 值一致性极高）；`prompt_ms` / `predicted_ms` 已在报告里，可直接做提速比与线性拟合。注意 restore 端耗时是 `restore_ms`（本例 18~42 ms）。 |
| **T-06** | 接口形态按 T-02 定为**单步 restore**；本票补充：**段 2 的 prompt 必须是 token 数组**，Engine Adapter 需要保留「已生成 token id」的通道，不能只留文本。 |
| **T-07** | **不加 `n_ctx` 维度**（§5）。 |
| **T-09** | restore 前用 `meta.n_tokens ≤ server.n_ctx` 预筛（§5 第 5 条）；恢复后用 `n_read == meta.bytes` 交叉核对（承接 T-02 §6.4 的建议，本票实测 `n_read` 确实等于 `n_written`）。 |
| **T-13** | 本脚本可直接复用为 AC1 的验收执行体：退出码 0 = 通过，报告 JSON 含全部参数与两条轨迹。 |

---

## 7. 复现步骤

```bash
# 正式跑（N=512，K=256/511，含 ctx 探针）—— 约 2 分钟
python scripts/03_verify_exact.py --report reports/ac1-exact-match.json

# 快速自检（N=64，K=32/63）
python scripts/03_verify_exact.py --n-tokens 64 --ks 32,63 --report reports/ac1-selftest.json

# 只跑主链、跳过 ctx 探针
python scripts/03_verify_exact.py --ctx-probe ""
```

脚本自己拉起并杀掉 llama-server（**不要**另起服务占端口；默认监听 `8083`）。
退出码：`0` 通过 / `1` AC1 未通过 / `2` 执行异常。
服务日志落在 `reports/logs/ac1-server-p<port>-gen<N>.log`。

---

## 8. 变更记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-09-12 | 首版：AC1 通过（K=256/511 逐字一致）；补 `-c` 不匹配探针，结论「`-c` 不必一致」并解除 T-07 的 `n_ctx` 维度；记录三个观测坑 |
