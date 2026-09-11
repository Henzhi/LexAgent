# T-06 · Engine Adapter（/slots 客户端）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | T-05；契约以 [T-02](./T-02-slot-api-smoke.md) 实测为准（软依赖，可先用 mock 开工） |
| 阻塞 | T-09、T-10 |
| SPEC 依据 | REQ-E3、§4.2 组件 1、R6 |
| 预估 | 2~3 h |

## 要做出什么

把 `llama-server` 的 `/slots` 能力包成一层 Python 客户端：能 save / restore / 探活，并且把「服务器根本没有这个能力」变成**一个明确的异常**，而不是静默失败或含糊的 HTTP 错误。

这一层是纯 IO，**不做任何决策**——决策是 T-09 的事。

## 范围

**做**

- `kv_cache/engine.py`：`LlamaSlotClient(base_url, timeout)`
  - `health()` —— 探活
  - `capabilities()` —— **能力探测**：判断服务端是否配了 `--slot-save-path`。首次调用后缓存结论（带失效窗口），不要每次都探（R6：上游 API 未承诺稳定；同时省 RTT）
  - `save(filename)` / `restore(filename)` —— 目标 slot 的选择**严格按 T-02 实测语义**（若 T-02 结论是「必须先占位」，这里就实现两步）
  - 错误映射：**501 → `SlotApiUnavailable`**；其它非 2xx → `SlotApiError`（**保留原始响应体**）；超时 → 独立异常或 `SlotApiError` 带 `timeout=True`
  - `SlotApiUnavailable` 的消息必须**人可读且指向根因**，例如提示「服务端未配置 `--slot-save-path`」——这是 REQ-E3 的实质要求
- `tests/test_engine.py`：用 `httpx.MockTransport`（或等价手段）覆盖
  - 200 正常
  - **501**
  - 500 / 404 / 畸形 JSON / 空响应体
  - 超时
  - 能力探测的缓存行为（一次失败后不无限重探）

**不做**

- 不做降级决策、不吞异常（T-09 决定「要不要继续」）
- 不做重试 —— 先不加复杂度，等真需要了再加
- 不实现 token 化（那是 T-07 的输入侧）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/kv_cache/engine.py` | `/slots` 客户端 |
| `kv-cache-resume/tests/test_engine.py` | 全离线单测（无真实网络） |

## 验收清单

- [ ] 501 抛出 `SlotApiUnavailable`，消息含 `--slot-save-path` 提示（人可读，REQ-E3）
- [ ] 其它非 2xx 抛 `SlotApiError` 且**带原始响应体**（不丢现场，后面定位全靠它）
- [ ] 能力探测结论被缓存，**不会每次调用都重探**（断言请求次数）
- [ ] 单测全离线：`pytest tests/test_engine.py -q` 不产生任何真实网络请求
- [ ] 目标 slot 的选择逻辑与 T-02 findings **逐条对应**（在代码注释里引用 findings 的结论编号）
- [ ] （可选）打一个 `@pytest.mark.integration` 的真实 server 冒烟，默认不跑

## 备注

- T-02 的 findings 是本票的契约。**如果 T-02 还没做完**，先按 SPEC §3.1 的转述实现，打 `# TODO(T-02)` 标记，等 findings 出来再对齐——但**不要跳过对齐**，实测与文档不一致时以实测为准。
- R6 提到上游 API 未承诺稳定 → 本层是**唯一**碰 HTTP 的地方，将来 llama.cpp 改 API 只改这一个文件。这个边界要守住，不要在别处直接发 HTTP。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-09](./T-09-hit-and-restore.md) / [T-10](./T-10-persist-and-save.md)
