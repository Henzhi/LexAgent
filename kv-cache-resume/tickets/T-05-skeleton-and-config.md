# T-05 · 策略层骨架与 KV_* 配置

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | 无 —— 可立即开始（与 E0 并行） |
| 阻塞 | T-06、T-07、T-08、T-09、T-10、T-11、T-12、T-13 |
| SPEC 依据 | G3、§4.2、§7 Phase 1 |
| 预估 | 2~3 h |

## 要做出什么

一个能 import 的空壳包 + 一套集中配置 + 一张**命中判断状态机图**。

后面八张票都往这个骨架里填一块，所以本票的产出是**契约**：接口签名定错了，后面每张票都要返工。状态机图比代码重要。

## 范围

**做**

- `kv_cache/` 包结构：
  - `config.py` —— 全部 `KV_*` 环境变量，**每个都有默认值**，非法值给明确报错（不是静默取默认）：
    `KV_CACHE_DIR` / `KV_ENABLED` / `KV_MAX_BYTES` / `KV_MAX_ENTRIES` / `KV_MIN_FREE_BYTES` / `KV_QUANT` / `VERIFY_EXACT` / `RESTORE_TIMEOUT_S` / `SERVER_BASE_URL`
  - `errors.py` —— 错误分类，**这是降级逻辑的依据，不能含糊**：
    `SlotApiUnavailable`（501）/ `SlotApiError`（其它非 2xx）/ `RestoreFailed` / `SaveFailed` / `PrefixMismatch`
  - `__init__.py` —— 导出公开 API 草图：`KVCachePolicy` 的接口签名（`lookup` / `persist` / `enforce_limits`）先定，**实现留空或 raise NotImplementedError**
  - `models.py` —— 跨模块的数据结构：`Decision`（HIT/MISS/UNCACHED）、`CacheMeta`、`TelemetryEvent` 的字段定义
- 独立测试与工程配置（**只在 `kv-cache-resume/` 内**）：
  - `pyproject.toml`（子目录级，依赖最小化：`httpx`、`pytest`、`ruff`）
  - `tests/conftest.py`
  - **决策并记录**：本目录用独立 venv 还是复用 LexAgent 根的 uv 环境（写进 `ENV.md`）
- `docs/phase1-design.md`：
  - 模块职责边界表（谁负责决策、谁负责 IO）
  - **命中判断状态机图**（请求进来 → key 计算 → 查索引 → 命中/未命中 → restore/冷 prefill → 失败回退）
  - 数据流图（对齐 SPEC §4.2）

**不做**

- 不实现任何策略逻辑（留给 T-06~T-12）
- 不改 LexAgent 根的 `pyproject.toml` / `src/` / `tests/`
- 不引入除 `httpx` / `pytest` / `ruff` 之外的依赖

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/kv_cache/__init__.py` | 公开 API 导出 |
| `kv-cache-resume/kv_cache/config.py` | `KV_*` 配置（含校验） |
| `kv-cache-resume/kv_cache/errors.py` | 错误类型 |
| `kv-cache-resume/kv_cache/models.py` | 数据结构契约 |
| `kv-cache-resume/pyproject.toml` + `tests/conftest.py` | 工程骨架 |
| `kv-cache-resume/docs/phase1-design.md` | **状态机图 + 模块边界**（本票核心交付） |

## 验收清单

- [x] `python -c "from kv_cache import KVCachePolicy"` 成功（骨架可导入）
- [x] `KV_*` 环境变量覆盖有单测，且**非法值报错清晰**（不静默吞掉）
- [x] 每个错误类型有 docstring 说明**什么情况下抛**，与 REQ-E3/W1/W3 的对应关系写清
- [x] `docs/phase1-design.md` 的状态机图覆盖全部出口分支（hit / miss / uncached / 失败回退），无「等等」之类未定义节点
- [x] ruff check + format 通过
- [x] `ENV.md` 补上「本目录 Python 环境怎么跑」一段

## 实施记录（2026-09-12）

| 项 | 结论 |
| :--- | :--- |
| Python 环境 | **复用根 `.venv`**（httpx 0.28.1 / pytest 9.1.1 / ruff 齐全），不新建 venv；未动根 `pyproject.toml` |
| 状态机出口 | 细化到 **10 个出口**（原票只要求覆盖 4 类），见 `docs/phase1-design.md` §2.1 |
| 配置项 | 票列 9 个 + **追加 `KV_CAPABILITY_TTL_S`**（T-06 能力探测缓存窗口，R6 需要），已在 design §3 备案 |
| 错误类型 | 票列 5 个 + 追加基类 `KVCacheError` 与 `KVCacheConfigError`（非法配置需独立类型才能「报错不静默」） |
| 测试 | `pytest kv-cache-resume/tests -q` → **89 passed** |
| 未做（留给后续票） | `lookup` / `persist` / `enforce_limits` 三个方法按票要求刻意 `raise NotImplementedError`，报错信息指向承接票号 |

## 备注

- **状态机图是本票的真正产出**。后面 T-09（命中判定）与 T-10（落盘）会有大量分支，图先画对，代码就只是翻译。
- 配置项默认值不要拍脑袋：`KV_MAX_BYTES` 等 T-04 的数据出来再定，本票先给保守默认（写清「待 T-04 校准」）。
- 若决定复用 LexAgent 的 uv 环境，注意**不要**在根 `pyproject.toml` 加依赖 —— 用子目录的 `pyproject.toml` + 独立 venv 更干净。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-06](./T-06-engine-adapter.md) / [T-07](./T-07-prefix-key.md) / [T-08](./T-08-index-store.md) 三张可并行开跑
