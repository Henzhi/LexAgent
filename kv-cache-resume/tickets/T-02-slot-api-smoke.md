# T-02 · slot API 冒烟 + 501 基线

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
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

- [ ] 带 `--slot-save-path`：save 返回 200，`kv/` 下出现 `.bin` 且字节数被记录
- [ ] 不带该 flag：save 与 restore 都返回 **501**，脚本能把它与 500/404 区分开
- [ ] `phase0-slot-api-findings.md` 写清了三个「必须实测」的问题，**每条附原始响应片段**
- [ ] 冒烟脚本重跑幂等（第二次跑不因文件已存在而失败）

## 备注

- **slot id 语义是本票最大的未知**。如果只有 slot 0 且会被回收，restore 就必须是「先占位 → 再 restore」两步——这会直接改写 T-06 的接口形态。**早暴露比晚暴露便宜得多**，所以本票要把它钉死。
- R5 已知坑：vision（mmproj）模型会**阻塞** slot-save（本项目无关，但要知道现象）；SWA 类模型需 `--swa-full` 否则保存不完整。
- 记录 build 号进 findings —— R6 说上游 API 未承诺稳定，将来坏了好回溯是哪个版本变的。

## 完成后

更新 [`README.md`](./README.md) 状态表 → 开始 [T-03](./T-03-cross-process-exact-match.md)；T-06 的接口按本票 findings 定
