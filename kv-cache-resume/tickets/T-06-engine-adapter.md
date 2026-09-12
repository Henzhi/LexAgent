# T-06 · Engine Adapter（/slots 客户端）

| 项 | 值 |
| :--- | :--- |
| 状态 | ✅ 完成（2026-09-12） |
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

- [x] 501 抛出 `SlotApiUnavailable`，消息含 `--slot-save-path` 提示（人可读，REQ-E3）
      —— 服务端消息本来就带 flag 名（C5），实现**转述原文**而不是另编一句
- [x] 其它非 2xx 抛 `SlotApiError` 且**带原始响应体**（不丢现场，后面定位全靠它）
      —— `status_code` / `body` / `url` / `timeout` 四个字段都带出；`test_400_keeps_raw_body` 断言原文可读
- [x] 能力探测结论被缓存，**不会每次调用都重探**（断言请求次数）
      —— `test_result_is_cached_second_call_does_not_reprobe` 断言 3 次调用只发 1 个请求；
      另有 TTL 过期 / `force=True` / `reset()` 三条路径的单测；**探测网络失败不缓存**（不把「连不上」记成「不支持」）
- [x] 单测全离线：`pytest tests/test_engine.py -q` 不产生任何真实网络请求
      —— 全部走 `httpx.MockTransport`；唯一真实网络用例标了 `@pytest.mark.integration`
- [x] 目标 slot 的选择逻辑与 T-02 findings **逐条对应**（在代码注释里引用 findings 的结论编号）
      —— C1~C9 逐条标注，含 C3（拒绝非法文件名）、C6（`trust_env=False`）、C7（必带 filename）、C8（slot id 自己解析）
- [x] （可选）打一个 `@pytest.mark.integration` 的真实 server 冒烟，默认不跑
      —— 已升级为**完整往返**（探活 → 能力 → slot → save → restore），并在真实实例上跑通过，见下方「实测证据」

## 实测证据（2026-09-12）

离线单测证明「按我理解的契约实现了」，**不证明「我理解的契约是对的」**，
所以对真实实例（8085，`n_ctx=2048`，与 T-02 那份配置不同）跑了一遍完整往返：

| 环节 | 实测 |
| :--- | :--- |
| `health()` | `True` |
| `capabilities()` | `available=True`，`probe_status=400`（判别式成立） |
| `resolve_slot_id()` | `0`（C1） |
| `save()` | 200，`n_saved=13`，`n_written=480348`，`save_ms=43.4`，端到端 49.5ms |
| `restore()` | 200，`n_restored=13`，`n_read=480348`，`restore_ms=39.0`，端到端 44.0ms |
| 落盘校验 | `kv/t06_live_probe.bin` = **480348 字节**，与 `n_written` 相同 |
| 副作用校验 | 探测跑完 `kv/` 里没有 `probe.bin` —— 判别式确实只读 |

**顺带交叉验证了 T-04 的 AC3 定律**：`36880 × 13 + 908 = 480348`，与实测 `n_written` 逐字节相等，
而这是**另一份 `-c` 配置**的实例 —— 说明该定律是结构性的，不是拟合巧合。

复现：`KV_TEST_SERVER=http://127.0.0.1:8085 pytest -m integration tests/test_engine.py`

## 落地时与票面不符之处（已回改文档）

1. **C3 的处置方式改了**：票/findings 原写「Adapter 侧做文件名净化」，实现改成**直接拒绝**
   （`is_valid_slot_filename()` → `ValueError`）。净化会把两个不同的键**静默**映射到同一个文件，
   正是 REQ-W2 要防的事。已在 findings 新增 §0.1 记录这处偏离，以代码为准。
2. **`-m "not integration"` 真的加进 `addopts` 了**：原来只在注释里声称「默认不跑」，
   实际靠「没设 `KV_TEST_SERVER` 所以 skip」——「不可能误跑」和「跑了刚好 skip」不是一回事。
3. **`capabilities()` 零 TTL 语义**：`capability_ttl_s <= 0` 解释为**永不过期**（不是「立即过期」）。
   写单测时差点按后者设想，已用 `test_zero_ttl_never_expires` 钉住。

## 备注

- T-02 的 findings 是本票的契约。**如果 T-02 还没做完**，先按 SPEC §3.1 的转述实现，打 `# TODO(T-02)` 标记，等 findings 出来再对齐——但**不要跳过对齐**，实测与文档不一致时以实测为准。
- R6 提到上游 API 未承诺稳定 → 本层是**唯一**碰 HTTP 的地方，将来 llama.cpp 改 API 只改这一个文件。这个边界要守住，不要在别处直接发 HTTP。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-09](./T-09-hit-and-restore.md) / [T-10](./T-10-persist-and-save.md)
