# T-03 · 跨进程续生成 + 逐字比对（AC1）

| 项 | 值 |
| :--- | :--- |
| 状态 | ✅ 完成（2026-09-12）—— **AC1 通过** |
| Epic | E0 · 机制验证（SPEC Phase 0） |
| 阻塞于 | T-02 |
| 阻塞 | T-04；T-13（软，脚本复用） |
| SPEC 依据 | G1、REQ-O1、AC1 |
| 预估 | 3~4 h |

## 要做出什么

**AC1 是整个 spike 的地基**：证明「生成 → save → 杀进程 → 重启 → restore → 续生成」出来的 token 序列，与「不中断一次跑完」完全一致。

这不是「差不多能接上」——是 **token 级完全相同**。对不上，后面的策略层再优雅也没有意义。

## 范围

**做**

`scripts/03_verify_exact.py`，两条轨迹 + 一次比对：

1. **基线轨迹**：一次不中断生成 N 个 token，记录完整 token id 序列（不是文本）与 sha256
2. **分段轨迹**：
   - 跑到第 K 个 token → `action=save`
   - **kill 掉 llama-server 进程**（真杀，不是重启 HTTP 连接）
   - 重新起服务 → `action=restore`
   - 继续生成到 N 个 token
3. **比对**：逐 token diff 两条轨迹；一致则给出共同前缀 sha256；不一致则给出**首个分歧位置**与两侧各 5 个 token 的上下文

采样参数**必须钉死**：`temperature=0` + 显式固定 `seed`。
> 理由：KV 里含 RNG 与 logits 状态（SPEC §3.1 的 `llama_state_seq_get_data` 说明）。采样参数漂移会让两条轨迹必然分叉，那是**脚本问题不是机制问题**，但会浪费你一整天。

**不做**

- 不做性能统计（T-04）
- 不接策略层（E1）；本票是**裸 API 驱动的验证链**，不 import `kv_cache/`
- 不覆盖多轮对话 / 工具调用场景（那是 Phase 3）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/scripts/03_verify_exact.py` | 验证链脚本，一条命令跑完 |
| `kv-cache-resume/reports/ac1-exact-match.json` | 原始数据：两条轨迹的 token ids、sha256、分歧位置（无分歧则为空）、耗时 |
| `kv-cache-resume/docs/phase0-ac1-report.md` | 结论 + 复现步骤 + 原始数据路径 |

## 验收清单

- [x] **AC1：token 级 100% 一致**，报告中含两条轨迹的 sha256 与完整参数（模型/量化/ctx/K/N/seed）
      → 基线 `abb798774502184244a5ed9d3192605f91fdae7301eb7120ef5df4adb1dc8b5b`；
      K=256 与 K=511 的拼接轨迹 sha256 **与基线完全相同**，共同前缀 512/512
- [x] 脚本可重复执行，重跑结论一致（幂等）
      → 自检 2 次 + 正式 3 次，共 5 次独立运行，基线 sha256 逐字相同（跨脚本版本、跨进程）
- [x] 脚本确实**杀了进程**（有进程 pid 日志与 kill 记录），不是假装重启
      → 报告 `process_lifecycle` 记录 5 个进程的 pid / 存活时长 / 退出码 / kill 方式；
      被杀 pid 与重启 pid 不同（27144→29244、27272→15420）；kill 后断言端口真的释放
- [x] 若不一致：报告指出首个分歧 token 位置 → **本票视为未完成**
      → 无分歧，`first_divergence = null`
- [x] 换一个 K 值（例如 K = N/2 与 K = N-1）两次都一致 —— 单点一致可能是巧合
      → K=256（N/2）与 K=511（N-1）均一致

### 额外交付（超出票面，标为改进项）

- [x] **结掉 T-02 移交的未验证前提**：实测 `-c` **不必一致**，约束是「恢复时 `n_saved ≤ n_ctx`」
      → **解除 T-07 的 `n_ctx` 键维度**（若按「必须一致」实现会白白让命中率归零）
- [x] 新增三项观测纪律（都曾导致误判，已写进 findings）：
      ① 回灌必须用 **token id 数组**，拼文本会往返失真；
      ② **`tokens_evaluated` 不是命中指标，`timings.cache_n` 才是**；
      ③ **「端口可连」≠「服务就绪」**，必须轮询 `/health`
- [x] `reports/ac1-exact-match.json` 纳入版本控制（42.8 KB）—— AC1 是地基结论，原始 token 数组应可复查；
      `.gitignore` 改为「`reports/*` 全忽略 + AC 证据白名单」，`kv/`、`*.bin`、`*.gguf` 仍绝不入库

## 备注

不一致时按顺序查这三处，别乱试：

1. **chat template 是否在重启后重新渲染**，引入空白/换行差异 —— 这是 R1 的头号嫌疑
2. **`seed` 与采样参数两边是否真的一致**（包括服务端默认值与请求体覆盖的优先级）
3. **模型是否需要 `--swa-full`**（R5：SWA 类模型不设这个，保存的是不完整的 KV）

另外注意 **`K` 与 `N` 的选择**：K 要足够大以覆盖一个完整的前缀（比如 512 token），N 到 1K 量级即可，本票验的是**一致性**不是长度极限。

## 完成后

更新 [`README.md`](./README.md) 状态表 → 开始 [T-04](./T-04-perf-and-size.md)

---

## 实测结论（详见 [`../docs/phase0-ac1-report.md`](../docs/phase0-ac1-report.md)）

| # | 结论 | 对下游的影响 |
| :--- | :--- | :--- |
| A1 | **AC1 成立**：跨进程续生成 token 级逐字一致 | 机制地基可用 |
| A3 | 段 2 `cache_n` **等于** `save` 的 `n_saved`（268/268、523/523） | restore 不是「假装成功」 |
| A4 | 回灌必须用 **token id 数组** | T-06 必须保留「已生成 token id」通道，不能只留文本 |
| A6 | `-c` 不必一致；约束是**恢复时 `n_saved ≤ n_ctx`** | **T-07 不加 `n_ctx` 维度**；T-09 用 `meta.n_tokens ≤ server.n_ctx` 预筛 |
| A7 | KV 体积 **36,883 / 36,882 B/token** | 交 T-04 做 AC3 线性拟合 |

> T-04 需要的原始耗时（`prompt_ms` / `predicted_ms` / `restore_ms`）都已在报告 JSON 里，不必重跑。
