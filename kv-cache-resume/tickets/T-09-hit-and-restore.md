# T-09 · 命中判定与 restore 全流程（AC5）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | T-06、T-07、T-08 |
| 阻塞 | T-12、T-13 |
| SPEC 依据 | REQ-E2、REQ-E4、REQ-U2、REQ-W1、REQ-W2、AC5 |
| 预估 | 4~5 h |

## 要做出什么

一次请求进来，策略层给出决定：**命中就 restore，未命中就冷 prefill**。命中时更新使用统计；**任何 restore 失败都必须照常冷 prefill 把请求做完**。

这是整个策略层的咽喉——T-05 画的状态机在这里落成代码。

## 范围

**做**

- `kv_cache/policy.py`：`KVCachePolicy.lookup(tokens, model_id, quant) -> Decision`
  - 分支必须覆盖 T-05 状态机的**每一个出口**，不允许「等等」式的隐含分支：
    - `UNCACHED` —— 无缓存模式（服务端 501），**不发起任何 restore 调用**
    - `HIT(filename)` —— 键命中 + 元数据校验通过 + restore 成功
    - `MISS(reason)` —— 未命中 / 校验失败 / restore 失败，每种给**明确 reason 枚举**
  - **restore 前校验模型与量化**（REQ-U2）：不匹配 → **丢弃该条目**（删文件 + 摘索引）→ 返回 MISS
  - **失败回退**（REQ-W1）：restore 抛 `SlotApiUnavailable` / `SlotApiError` / 超时 → 捕获 + 告警日志 → MISS，**绝不向调用方抛异常**
  - **前缀校验**（REQ-W2）：restore 后核对 token 序列（能核对则核对；不能核对的情形要显式写清并降级为「按 key 可信」）
  - **命中计数**（REQ-E4）：HIT 后更新 `hits` +1、`last_used_at` 前进
  - 决策结果**不带副作用地可测**：注入的 client / store 全部构造参数化
- `tests/test_policy_lookup.py`：**每个 REQ 一条正向 + 一条反向**，重点是三个失败注入（AC5）
  - 注入 501 → UNCACHED 且后续不再调 restore（断言调用次数 0）
  - 注入超时 → MISS 且不抛
  - 注入 500 → MISS 且不抛
  - 校验失败路径：断言**文件真的被删了**（不只是返回 MISS）

**不做**

- 不实现落盘（T-10）、不做淘汰（T-11）、不发遥测事件本体（T-12，本票只留 hook 点）
- 不做重试

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/kv_cache/policy.py` | `lookup` 部分 |
| `kv-cache-resume/tests/test_policy_lookup.py` | 含 AC5 三种失败注入 |

## 验收清单

- [ ] **REQ-E2**：同键第二次请求走 restore 分支（断言 engine 的 restore 被调用）
- [ ] **REQ-W1 / AC5**：注入 501 / 超时 / 500 三种失败，`lookup` **都不抛异常**且返回 MISS
- [ ] **REQ-U2**：`model_id` / `quant` 不匹配 → 条目被丢弃（断言文件被删 + 索引条目消失）+ 返回 MISS
- [ ] **REQ-E3**：探测到 501 后，**不再发起 restore 调用**（断言调用次数为 0）
- [ ] **REQ-E4**：命中后 `hits` +1、`last_used_at` 前进（断言时间戳真的变了）
- [ ] `MISS(reason)` 的 reason 是枚举，覆盖：`not_found` / `model_mismatch` / `quant_mismatch` / `restore_failed` / `restore_timeout` / `prefix_mismatch`
- [ ] 状态机与 `docs/phase1-design.md` 逐分支对应（**图中每个出口都有测试**，不许有孤儿分支）

## 备注

- **「失败不抛」不是「失败静默」**：MISS 必须带原因，且必须进告警日志。SPEC §4.1 的对称项是 LexAgent 的「日志故障只告警，不阻断主链路」——告警还是要发，只是不阻断。
- AC5 的验收姿势是「**强制失败**」：不要在测试里靠随机网络抖动，要用注入把 restore 钉死为失败，这样才能覆盖到每一次。
- 本票不写 `persist`（T-10 写）。但要在 `policy.py` 里把两个方法的**共享状态**（索引句柄、锁）一起初始化好，避免 T-10 再动构造函数。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-10](./T-10-persist-and-save.md) / [T-11](./T-11-eviction-and-watermark.md) / [T-12](./T-12-telemetry.md)
