# Phase 0 · slot API 实测记录（T-02）

| 项 | 值 |
| :--- | :--- |
| 日期 | 2026-09-12 |
| 引擎 | llama.cpp **build 10809**（`0.4.0-dev`, commit `5266f24da`） |
| 服务端 | `llama-server -m <qwen2.5-3b Q4_K_M> -ngl 99 -c 4096 -np 1 --slot-save-path <kv>` |
| 隔离 | 两个实例：`8080`（**有** `--slot-save-path`）/ `8081`（**无**） |
| 探针脚本 | [`scripts/slot_smoke.py`](../scripts/slot_smoke.py)（三模式：`smoke` / `restore-only` / `expect501`） |
| 原始数据 | `reports/t02-smoke.json`、`reports/t02-501.json`、`reports/t02-restore-cold.json`（`reports/` 已 gitignore） |
| 需求依据 | SPEC §3.1、REQ-E3、REQ-O3、R5、R6 |

> 本文只写**实测**，不转述 SPEC。与 SPEC 冲突处以本文为准（`tickets/README.md` 的约定）。
> 结论对 T-06（Engine Adapter）是**契约**，代码里的 slot 选择逻辑必须与本文逐条对上。

---

## 0. 结论速查（T-06 直接照着实现）

| # | 结论 | 影响 |
| :--- | :--- | :--- |
| C1 | **目标 slot 是 `0`**（`-np 1` 时 `/slots` 只返回 1 个槽位）。slot id **不随请求变化** | 单步调用即可，无需「先占位」 |
| C2 | **空闲 slot 可直接 restore**，不必先预热占用（全新进程内已验证） | `restore` 是单次 POST，不是两步。⚠️ 前置条件「`-c` 与写盘时一致」未验证，见 §3 末 |
| C3 | `filename` **只接受纯文件名**；带子目录、`/`、`\`、`..` 一律 **400 `Invalid filename`** | Adapter 侧自己做文件名净化，别指望服务端 |
| C4 | 同名重复 save **覆盖**（返回 200，且文件 **mtime 已更新** —— 是真重写而非跳过） | 与 `REQ-E1` 的「同 key 覆盖写、`hits` 不清零」自然吻合 |
| C5 | **不配 `--slot-save-path` → save/restore 都返回 501**，且报错消息自己就写了这个 flag 名 | REQ-E3 的「显式报错」几乎白送，Adapter 转述即可 |
| C6 | **HTTP 客户端必须 `trust_env=False`** —— 否则沙箱代理会让请求 **200/404 交替**（见 §5） | 不处理这条，整个策略层会随机假失败 |
| C7 | 请求体**缺 `filename` 会 500**（服务端不做 body 校验，直接抛异常） | Adapter 必须始终带 `filename` |
| C8 | **越界 slot id 也返回 200**（`id_slot:9` 照样 `n_saved:20`） | 不要相信「slot id 错了会被拒」；slot id 由我们自己保证 |
| C9 | restore 成功**无法从 `/slots` 观测**（`n_past` 等字段不出现） | T-09 的 REQ-W2 前缀校验不能靠 `/slots`，见 §6 |

---

## 1. 端点清单（实测状态码）

| 端点 | 方法 | 实测 | 备注 |
| :--- | :--- | :--- | :--- |
| `/health` | GET | **200** `{"status":"ok"}` | 探活可用 |
| `/props` | GET | **200** | 返回默认采样参数（含 `temperature`、`seed:4294967295` 等） |
| `/slots` | GET | **200**（需要 `trust_env=False`） | 见 §2 |
| `/slots/{id}?action=save` | POST | **200** | body `{"filename":"x.bin"}` |
| `/slots/{id}?action=restore` | POST | **200** | 同上 |
| `/slots/{id}?action=<非法>` | POST | **400** `Invalid action` | **仅当配了 `--slot-save-path`**；没配时是 501 |
| `/slots/99999` | GET | **404** `File Not Found` | 非数字路径段走通用 404 |
| `/completion` | POST | **200** | 支持 `id_slot` / `cache_prompt` |

### 1.1 原始响应片段（save / restore）

> 下列片段取自 `reports/t02-smoke.json`。**`timings` 逐次运行会浮动**（同一台机、同一进程内 save_ms 实测
> 17~47ms、restore_ms 4~49ms，受 GPU/CPU 抢占影响），**结构性字段才是契约**：
> `n_saved` / `n_written` / `n_restored` / `n_read` 与 `id_slot` / `filename`。定量数据归 T-04。

**save（有 flag）**
```json
{"id_slot":0,"filename":"smoke.bin","n_saved":20,"n_written":738508,"timings":{"save_ms":25.826}}
```

**restore（有 flag，slot 预热过）**
```json
{"id_slot":0,"filename":"smoke.bin","n_restored":20,"n_read":738508,"timings":{"restore_ms":48.526}}
```

**restore（冷启动后、空前闲 slot —— Q2 关键证据）**
```json
{"id_slot":0,"filename":"smoke.bin","n_restored":20,"n_read":738508,"timings":{"restore_ms":3.901}}
```

**501（无 flag，save 与 restore 都是这一条）**
```json
{"error":{"code":501,"message":"This server does not support slots action. Start it with `--slot-save-path`","type":"not_supported_error"}}
```

> 服务端自己把 `--slot-save-path` 写进了消息 —— REQ-E3 要求「人可读且指向根因」，
> 转述这条原文即达标，不需要 Adapter 再编一句话。

---

## 2. Q1 · `GET /slots` 语义

**请求前：**
```json
[{"id":0,"n_ctx":4096,"speculative":false,"is_processing":false}]
```

**一次生成之后：**
```json
[{"id":0,"n_ctx":4096,"speculative":false,"is_processing":false,
  "id_task":20,"n_prompt_tokens":20,"n_prompt_tokens_processed":0,"n_prompt_tokens_cache":0,
  "params":{"seed":42,"temperature":0.0, ...}}]
```

三问三答：

1. **返回什么**：一个数组，元素是槽位对象。基础字段 `id` / `n_ctx` / `speculative` / `is_processing`；
   槽位被用过之后才追加 `id_task` / `n_prompt_tokens*` / `params`。
2. **有几个**：`-np 1` 时 **1 个**（`id=0`）。槽位数由服务端 `-np`（`--parallel`）决定，不是固定的 4 或 8。
3. **id 是否随请求变化**：**不变**。预热前后两个快照都是 `id=[0]`。
   `id_task` 会随着请求递增，但那是**任务号**不是槽位号 —— 别把 `id_task` 当 slot id 用。

---

## 3. Q2 · restore 的前置状态

**结论：空闲 slot 可以直接 restore，不需要先占位。**

做法：起一个**全新进程**（`--port 8082`，与写盘时同参数 `-c 4096`，**未做任何生成**，
故 `/slots` 只有基础字段、无 `id_task`/`params`），直接 `POST /slots/0?action=restore`：

```
# 冷启动快照（证明该进程从未生成过任何 token）
[{"id":0,"n_ctx":4096,"speculative":false,"is_processing":false}]

# 直接 restore
HTTP 200 {"id_slot":0,"filename":"smoke.bin","n_restored":20,"n_read":738508,"timings":{"restore_ms":14.845}}
```

**对 T-06 的意义**：接口形态是**单步 POST**，不是「先发一次空请求占住槽位 → 再 restore」的两步流程。
票面备注担心的那个风险（「如果只有 slot 0 且会被回收，restore 就必须先占位」）**实测不成立**。

> 注意别把这条与 T-03 的 AC1 混为一谈：本节只证明「能装进去」，
> 至于「装进去之后续生成的 token 序列是否与不中断轨迹逐字一致」，那是 T-03 的事。

**⚠️ 本节结论成立的前置条件有一条未验证**：本次恢复时服务端的 `-c` **与写盘时一致**（都是 4096）。
KV 文件是否绑定了保存时的 `n_ctx`、`-c` 变小后 restore 会不会失败/静默出错，**本票未测**。
复核过程中确实观察到：用 `-Ctx 2048` 起服务时无法得到可信结论，故改用匹配配置重做。

> **✅ 已由 T-03 结项（2026-09-12）**：**`-c` 不必一致**。在 `-c 4096` 下 save（`n_saved=268`），
> 换 `-c 512` 重启后 restore **返回 200 且续生成与基线逐字一致**；换 `-c 128`（装不下）才返回 400。
> 真正的约束是**恢复时 `n_saved ≤ n_ctx`**，不是两端 `-c` 相等。
> 故 **T-07 无需把 `n_ctx` 计入键**；T-09 用 `meta.n_tokens ≤ server.n_ctx` 预筛即可。
> 详见 [`phase0-ac1-report.md`](./phase0-ac1-report.md) §5。

---

## 4. Q3 · `filename` 语义

| 传入 | 结果 | 落盘 |
| :--- | :--- | :--- |
| `smoke.bin` | **200** | `kv/smoke.bin`（738,508 B） |
| `sub\nested.bin` | **400** `Invalid filename` | 无 |
| `sub2/nested2.bin` | **400** `Invalid filename` | 无 |
| `../escape.bin` | **400** `Invalid filename` | 无 |

**结论**：

- 只接受**纯文件名**（相对路径不接受），**不能带子目录**，**路径穿越被拒**。
- **覆盖**：同名重复 save 返回 200。判定**不靠字节数**（KV 内容相同，字节数本来就不会变 ——
  「字节数一致」既可能是覆盖、也可能是服务端直接跳过，无法区分）：脚本记录目标文件的 **mtime**，
  实测 `mtime` 由 `1789196461869763500` 变为 `1789196461958916600` → **确实重写了文件**，不报错也不追加。
- 因此**「路径要隔离」的责任在客户端**：服务端只保证不越界，不保证不冲突。
  T-08 的 `slotcache_{key}.bin` 命名方案（前缀隔离）是必须的，不能省。

原始报错：
```json
{"error":{"code":400,"message":"Invalid filename","type":"invalid_request_error"}}
```

---

## 5. ⚠️ 环境陷阱：沙箱代理让请求 200/404 交替

**这是本票最有价值的发现，且极易误判成引擎 bug。**

本机环境注入了 `HTTP_PROXY=http://127.0.0.1:63791`。httpx 默认 `trust_env=True`，
于是**连 `127.0.0.1` 的请求也被交给代理**，而代理在 keep-alive 复用连接上会出现帧错位。

同一 `httpx.Client` 连发 5 次，症状是**严格交替**：

```
trust_env=True  : [200, 404, 200, 404, 200]
trust_env=False : [200, 200, 200, 200, 200]
```

404 的响应体是通用路由未命中：
```json
{"error":{"message":"File Not Found","type":"not_found_error","code":404}}
```

**排除过程**（记录在案，避免下次重新怀疑引擎）：

| 验证 | 结果 |
| :--- | :--- |
| `curl /slots` 单发 | 200 |
| 单 requests → `/slots` | 200 |
| httpx 连发 5 次（`trust_env=True`） | 200, **404**, 200, **404**, 200 |
| httpx 连发 5 次（`trust_env=False`） | 200 × 5 |
| `/health` 带 `Connection: close` 后再请求 | 200（因为换了新连接） |

**结论**：**不是 llama.cpp 的问题**，是代理层。llama.cpp 的 keep-alive 是正常的。

**对 T-06 的硬要求**：`LlamaSlotClient` 必须 `trust_env=False`。
否则每第二个请求就 404 → 被 T-06 映射成 `SlotApiError` → 被 T-09 记成 MISS →
表现为「缓存命中率永远只有一半」，而且**排查时根本不会怀疑到代理头上**。

> 这条也解释了为什么「看起来时好时坏」：所有单发请求（如人工 curl）都正常，
> 只有自动化连发才会暴露。

---

## 6. 其它必须记录的坑

### 6.1 缺字段直接 500（服务端不校验 body）

```json
{"error":{"code":500,"message":"[json.exception.out_of_range.403] key 'filename' not found","type":"server_error"}}
```

这是**请求方的 bug**，但服务端用 500 表达 —— 会被我们的分类器归到 `SERVER_ERROR`。
Adapter 必须**始终带 `filename`**，不要把「忘了传」混成「服务端故障」。

复现：`python scripts/slot_smoke.py smoke --probe-edge`（探针「save(body 缺 filename)」，期望 500）。

### 6.2 越界 slot id 静默「成功」

`POST /slots/9?action=save`（`/slots` 只有 `id:0`）与 `POST /slots/9999?action=save` 都返回：

```json
{"id_slot":9999,"filename":"smoke.bin","n_saved":20,"n_written":738508,"timings":{"save_ms":42.603}}
```

**200，且字节数与 slot 0 一模一样。** 响应体里的 `id_slot` 是**请求里的 id 原样回显**——
既不报错、也不改写、也不提示「你传错了」。

> ⚠️ 首版本文在此处曾推断「服务端把越界 id 夹取到了有效槽位」。**补做边界探针后该推断不成立**：
> 回显 `id_slot:9999` 说明它只是把请求参数抄了回来，**响应体无法证明它究竟写到了哪个槽位**。
> 是夹取、还是凭空建了个 9999 号槽位，从 HTTP 层面不可知 —— 结论反而更强：
> **这条路径没有任何可依赖的反馈**。

**含义**：**不能靠「传错 slot id 会报错」来兜底**。slot id 必须由我们自己从 `/slots` 取（结论 C1：恒为 0），
Adapter 不允许把调用方传进来的任意 id 直接转发。

复现：`python scripts/slot_smoke.py smoke --probe-edge`（探针「save(slot_id=9999 越界)」）。

### 6.3 restore 不存在的文件 → 400，且消息有误导性

```json
{"error":{"code":400,"message":"Unable to restore slot: No available space in KV cache or invalid slot save file","type":"invalid_request_error"}}
```

文件**根本不存在**，消息却说「空间不足**或**文件无效」。两种成因混在一句话里，无法据此判断根因。

**含义**：T-09 对 restore 的 400 只当作 `MISS(restore_failed)`（REQ-W1 的失败回退），
**不要去解析 message 猜原因**；真要定位，白盒检查是自己的 `store` 该做的事。

复现：`python scripts/slot_smoke.py smoke --probe-edge`（探针「restore(不存在的文件)」，期望 400；
脚本前置断言该文件确实不在 `kv/` 下，否则计入 failures）。

### 6.4 restore 成功无法从 `/slots` 观测

冷启动 restore 成功后，`GET /slots` 仍然只有基础字段：

```json
[{"id":0,"n_ctx":4096,"speculative":false,"is_processing":false}]
```

`n_past` / `n_prompt_tokens` **不会**因为 restore 而出现（只有真正跑过一次生成才有）。

**含义**：T-09 的 REQ-W2「restore 后核对 token 序列」**不能通过 `/slots` 实现**。
可选路径有三条，T-09 需择一并在设计文档写清：

1. 只信 `hash(完整 token 序列)` 的键（SPEC §4.3 的设计本意）—— 实现最简，但放弃了 restore 后的二次校验；
2. 用一次极小的探针生成比对 logits/输出 —— 代价是多一次 decode；
3. 记录并依赖服务端返回的 `n_restored` / `n_read` 与本地 meta 的 `n_tokens` / `bytes` 交叉核对 ——
   **本票建议走这条**：`n_read` 与 meta 里的 `bytes` 应当相等（本例都是 738508），
   成本为零且能挡住「文件被截断 / 版本不对」这类损坏。

### 6.5 R5 已知坑的复核

- **vision（mmproj）模型阻塞 slot-save**：本次用纯文本模型，**未复现**（save 25.8ms 正常返回）。
- **SWA 类模型需 `--swa-full`**：Qwen2.5 非 SWA 架构，**不适用**。若将来换模型需重新核对（记入 T-13 遗留）。

---

## 7. 与 SPEC 的出入

| SPEC 处 | SPEC 说法 | 实测 | 处置 |
| :--- | :--- | :--- | :--- |
| §3.1 | 「不设该 flag 时 save/restore 返回 501」 | ✅ 成立 | 一致 |
| §3.1 | `POST /slots/<id>?action=save`，body `{"filename":"x.bin"}` | ✅ 成立 | 一致 |
| T-02 备注 | 「若只有 slot 0 且会被回收，restore 必须两步」 | ❌ 不成立，空闲 slot 可直接 restore | 按实测：**单步** |
| T-02 备注 | R5「vision 模型阻塞 save」 | 未涉及（纯文本模型） | 记录待将来核对 |

---

## 8. 复现步骤

```bash
# 1. 起服务（有 slot 落盘）
powershell -ExecutionPolicy Bypass -File scripts/start_server.ps1

# 2. 冒烟 + Q1/Q3 + §6 边界（含 filename 语义、越界 slot id、缺 filename、缺文件 restore 探针）
python scripts/slot_smoke.py smoke --kv-dir kv --report reports/t02-smoke.json --probe-filename --probe-edge

# 3. 501 基线：另起一个不配 --slot-save-path 的实例
powershell -ExecutionPolicy Bypass -File scripts/start_server.ps1 -NoSlotSave -Port 8081
python scripts/slot_smoke.py expect501 --base-url http://127.0.0.1:8081 --report reports/t02-501.json

# 4. Q2：重启服务（槽位清空）后直接 restore
python scripts/slot_smoke.py restore-only --kv-dir kv --report reports/t02-restore-cold.json
```

三个模式都会写 JSON 现场（含每次请求的状态码、分类、响应体原文），并在失败时以非 0 退出。
`--probe-filename` / `--probe-edge` 两个开关带**期望值断言**：Q3 的 400、§6 的 500/400 任一不符即计入 `failures` 并非 0 退出 ——
即本文 §4、§6 的每条结论都能被机器复检，不依赖人工比对。

---

## 9. 变更记录

| 日期 | 变更 |
| :--- | :--- |
| 2026-09-12 | 首版：Q1/Q2/Q3 三问实测 + 501 基线 + 代理陷阱 + 4 条附带坑 + 对 T-06 的 9 条契约结论 |
| 2026-09-12 | 补做 §6 边界探针（`--probe-edge`）并**修正 §6.2 的错误推断**：越界 slot id 是原样回显而非夹取；§4/§6 结论加期望值断言与复现命令；§1.1 标注 timings 非契约 |
| 2026-09-12 | §4 覆盖语义改用 **mtime 断言**实证（原「字节数一致」不足以区分覆盖与跳过）；§3 Q2 在**全新进程**上复现，并暴露未验证前置条件「保存/恢复时 `-c` 须一致」→ 移交 T-03（可能影响 T-07 的键维度） |
