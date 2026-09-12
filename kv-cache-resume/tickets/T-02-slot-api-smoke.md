# T-02 · slot API 冒烟 + 501 基线

| 项 | 值 |
| :--- | :--- |
| 状态 | ✅ 完成（2026-09-12） |
| Epic | E0 · 机制验证（SPEC Phase 0） |
| 阻塞于 | T-01 |
| 阻塞 | T-03；T-06（软，提供契约实测） |
| SPEC 依据 | §3.1、REQ-E3、REQ-O3、R5、R6 |
| 预估 | 2~3 h |

## 要做出什么

一条命令起 `llama-server`，对着它能手工完成一次 slot save + restore，并**亲眼看到不设 `--slot-save-path` 时返回 501**。

顺带把槽位 API 的真实行为写成一份实测记录 —— 这是 T-06（Engine Adapter）唯一的契约来源，**不要照抄 SPEC 的转述，要写实测**。

## 范围

**做**

- `start_server.ps1`：`llama-server -m <model> --slot-save-path ./kv -ngl 99 -c <ctx>`，参数可配；把 `--cache-type-k/v`（KV 量化，REQ-O3）预留成可选开关
- `slot_smoke.py`：
  1. `GET /health` 探活
  2. 发一次生成（`/completion` 或 `/v1/chat/completions`）
  3. `POST /slots/<id>?action=save`，body `{"filename":"smoke.bin"}`
  4. 检查 `kv/smoke.bin` 落地，记字节数
  5. `POST /slots/<id>?action=restore`，body 同上
  6. 打印每一步的状态码与响应体
- **负例（AC5 的依据）**：不带 `--slot-save-path` 起一次服务，证明 save/restore 返回 **501**，并确认这个 501 与其它错误可区分
- **必须实测并写清的三件事**：
  - `GET /slots` 返回什么、有几个 slot、`id` 是否随请求变化
  - restore 时目标 slot 处于什么状态才允许（需要先占位？还是空闲 slot 直接 restore？）
  - save 的 `filename` 是否只接受文件名（相对路径）、能否带子目录、是否会覆盖

**不做**

- 不跨进程（杀进程是 T-03 的事）
- 不做 LRU / 容量治理
- 不做性能统计（T-04）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/scripts/start_server.ps1` | 服务启动脚本（含量化/上下文参数） |
| `kv-cache-resume/scripts/slot_smoke.py` | 冒烟脚本，可重复执行 |
| `kv-cache-resume/docs/phase0-slot-api-findings.md` | **实测记录**：端点、参数、状态码、slot id 语义、`/slots` 元数据字段、已知坑 |

## 验收清单

- [x] 带 `--slot-save-path`：save 返回 200，`kv/` 下出现 `.bin` 且字节数被记录
      → `n_saved=20`、`n_written=738508`，`kv/smoke.bin` 738,508 B
- [x] 不带该 flag：save 与 restore 都返回 **501**，脚本能把它与 500/404 区分开
      → `expect501` 模式断言 classification 必须为 `SLOT_API_UNAVAILABLE`；另设两个对照样本
      （非法 action 也是 501、`/slots/99999` 是 404），证明 501 是**路由级**而非 action 级
- [x] `phase0-slot-api-findings.md` 写清了三个「必须实测」的问题，**每条附原始响应片段**
      → §2 附请求前/后两段 `/slots` 原文；§3 附冷启动快照 + restore 原文；§4 附四组 filename 结果与 400 原文
- [x] 冒烟脚本重跑幂等（第二次跑不因文件已存在而失败）
      → 本轮连续重跑 3 次均通过，覆盖探测用 mtime 判定「真重写」

### 额外交付（超出票面，标为改进项）

- [x] §6 边界探针改为**可复现**：原 findings 的 §6.1/§6.2/§6.3 只有手工观察、脚本无法复跑；
      新增 `--probe-edge` 覆盖「缺 filename→500 / 越界 slot id→200 / restore 不存在文件→400」，带期望值断言
- [x] 据此**修正一处错误推断**：越界 slot id 并非「被夹取」，而是 `id_slot` **原样回显**（见 findings §6.2）
- [x] 覆盖语义改用 **mtime** 实证（原「字节数一致」无法区分覆盖与跳过）
- [x] Q2 在**全新进程**上复现，并暴露一条未验证前置条件：保存/恢复时 `-c` 须一致 → 已移交 T-03

## 备注

- **slot id 语义是本票最大的未知**。如果只有 slot 0 且会被回收，restore 就必须是「先占位 → 再 restore」两步——这会直接改写 T-06 的接口形态。**早暴露比晚暴露便宜得多**，所以本票要把它钉死。
- R5 已知坑：vision（mmproj）模型会**阻塞** slot-save（本项目无关，但要知道现象）；SWA 类模型需 `--swa-full` 否则保存不完整。
- 记录 build 号进 findings —— R6 说上游 API 未承诺稳定，将来坏了好回溯是哪个版本变的。

## 实测结论（T-06 契约，详见 findings）

| # | 结论 | 对下游的影响 |
| :--- | :--- | :--- |
| C1 | slot 恒为 `0`（`-np 1`），id **不随请求变化** | 单步调用，无需「先占位」 |
| C2 | 空闲 slot **可直接 restore**（全新进程已验证） | T-06 接口是**单次 POST** |
| C4 | 同名重复 save **覆盖**（mtime 已变，实证） | 与 REQ-E1「同 key 覆盖写、hits 不清零」吻合 |
| C5 | 无 `--slot-save-path` → save/restore **都 501**，消息自带 flag 名 | REQ-E3 的「显式报错」直接转述即可 |
| C6 | **HTTP 客户端必须 `trust_env=False`** | 不处理则缓存命中率**永远只有一半**，且极难怀疑到代理 |
| C7 | 请求体缺 `filename` → **500**（非 400） | Adapter 必须始终带 filename，别把「忘了传」混成服务端故障 |
| C8 | 越界 slot id → **200 且原样回显 id**，无任何提示 | 不能靠服务端兜底，slot id 必须自己从 `/slots` 取 |
| C9 | restore 成功**无法从 `/slots` 观测** | T-09 的前缀校验不能靠 `/slots`，建议用 `n_read` 与 meta `bytes` 交叉核对 |

## 完成后

更新 [`README.md`](./README.md) 状态表 → 开始 [T-03](./T-03-cross-process-exact-match.md)；T-06 的接口按本票 findings 定
