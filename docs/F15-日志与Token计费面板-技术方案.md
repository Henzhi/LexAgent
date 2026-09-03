# F15 日志与 Token 计费面板 — 技术方案

> 状态：方案定稿（2026-09-03）｜ 里程碑归属：M4 前置（M3 已全部收尾）
> 关联：F14 预算熔断（`src/observability/cost_budget.py`）、检索质量日志（`query_logs` 表）
> 目标读者：实现该功能的开发者 / AI Agent

## 1. 背景与定位

F14 已实现**按次数**的预算熔断（LLM / Tavily / pkulaw 每日调用上限，Redis 原子预占）。
但次数口径反映不了真实成本——一次 8K 上下文的复杂调用与一次 200 token 的简单调用成本差 40 倍。
F15 的目标是在 **F14 之外旁路新增一套 token/金额 观测层**，回答三个问题：

1. 每天/每周实际烧了多少钱（LLM token × 单价 + 外部 API 次数 × 单价）？
2. 钱花在了哪些环节（哪个模型 / 哪类工具 / 哪次会话 / 哪类场景）？
3. 外部 API 的免费积分额度还剩多少、按什么节奏在消耗？

**边界（刻意不做）**：
- 不改 F14 的熔断语义与存储（次数熔断继续实时、原子、便宜地工作）；
- 面板只读展示 + 价格表可编辑，**不提供**「按金额熔断」——金额依赖埋点事后统计，实时性不足以做熔断；
- 不做按用户分账（当前单实例/自用场景，`usage_logs` 预留 `user_id` 列即可）。

## 2. 决策记录

| # | 决策 | 选项 | 结论 |
|---|---|---|---|
| D1 | 面板受众 | 自用成本监控 / 多用户分账 / 对外展示 | **自用成本监控**。不做用户分账，表留 user_id 列 |
| D2 | token 数据源 | 补真实 usage / tiktoken 估算 / 混合 | **补真实 usage 埋点**（`response.usage_metadata`），拿不到（Ollama/流式无 usage）回退估算并打 `est` 标记 |
| D3 | 存储 | PG 明细表 / 复用 query_logs / 只读 Redis | **新增 PG `usage_logs` 明细表**，每次调用一行，SQL 聚合出趋势/归因 |
| D4 | 计价 | 价格表可配 | **config 默认值 → 首启灌入 PG `pricing` 表 → 前端可动态编辑**；落库时算好 `cost_cny` 快照，改价不漂移历史 |
| D5 | 每日 rollup | 定时任务 / 物化视图 / 直接查明细 | **先不建 rollup**：个人单实例日均几百行，PG GROUP BY 毫秒级。预留聚合查询为 `read_usage_summary()`，将来数据量大可换物化视图/定时表而不改 API |
| D6 | Tavily 计价口径 | 付费单价 / 免费额度 | 用户当前为**每月赠送免费积分**档；面板记录消耗的 credit 数与估算金额，标注「免费额度内」，价格表保留可改字段 |
| D7 | 北大法宝计价 | 统一按次 / 按工具区分 | **按工具类型区分积分**：基础关键词类 ≈25 积分/次，语义/识别类 ≈125 积分/次（官方计价见 §5）。单价（元/积分）留价格表可编辑 |
| D8 | 前端形态 | 并入主前端 | Vue3 新增 admin 计费页，复用现有 router/auth |

## 3. 埋点设计（三个位置，一个计价器）

### 3.1 LLM — 新增独立 UsageCallback

新建 `src/llm/usage_callback.py`，**不要**塞进 `LLMBudgetCallbackHandler`：
后者的 `on_llm_end` 语义是「不再计数」（配额已在 `on_llm_start` 预占，再记会把限额腰斩），
token 采集是另一个关注点，混在一起职责冲突且难测试。

```python
class LLMUsageCallbackHandler(BaseCallbackHandler):
    """旁路采集 token usage → 异步写 usage_logs。失败 debug 吞掉，绝不拖垮主链路。"""
    def on_llm_end(self, response, **kwargs):
        md = getattr(response, "usage_metadata", None) or {}
        if not md:  # Ollama / 流式未开 usage → 估算 + est 标记
            return self._record_estimated(response)
        # DeepSeek 特有：缓存命中价差 50 倍，需拆 cache hit/miss
        prompt_tokens = md.get("input_tokens", 0)
        # usage_metadata 未必含 cache 拆分 → 见 §3.2
        ...
```

挂载：与 `budget_callbacks()` 并列，`factory.py` 构造两个 ChatModel 时都挂
`callbacks=[*budget_callbacks(), *usage_callbacks()]`。
⚠️ 任何新增 LLM 后端都要同步挂，用测试 `test_callback_mounted_on_real_backends` 守住。

### 3.2 DeepSeek 缓存命中的价格差（重要）

官方刊例价（2026-09-03，¥/百万 tokens）：

| 模型 | 输入·缓存命中 | 输入·缓存未命中 | 输出 |
|---|---|---|---|
| deepseek-v4-flash | 0.02 | 1 | 2 |

缓存命中比未命中便宜 50 倍，而 ReAct 循环里 system prompt + 历史几乎每次都命中缓存。
因此**不能拿输入总 token 按未命中价算**（会高估近一倍）。

OpenAI 兼容 usage 里 DeepSeek 返回 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
采集策略（按顺序尝试，都不行则 `est=True` 且按未命中保守计价）：
1. `response.usage_metadata.input_token_details.cache_read`（LangChain 归一字段，若有）；
2. 从原始 `response.response_metadata` / `usage` 里读 `prompt_cache_hit_tokens`；
3. 都没有 → 视为全部未命中（保守），标 `est`。

`usage_logs` 同时存 `cache_hit_tokens` / `cache_miss_tokens` 两列，金额按拆分算：
`cost = hit/1e6×0.02 + miss/1e6×1 + output/1e6×2`。

### 3.3 Tavily — 在 search() 内埋点

`src/search/tavily.py` 的付费调用成功返回处记一行：`source=tavily, calls=1, credits=search_depth成本`。
basic=1 credit / advanced=2 credits。失败（含预算熔断 `ok=False`）不记。

### 3.4 北大法宝 MCP — 在 PkulawMCPClient._call 统一埋点

`src/search/pkulaw_mcp.py` 的 `_call()`（purpose: search/verify/…）**成功返回前**记一行：
`source=pkulaw, tool=purpose`。这是两条调用路径（Agent 工具 `pkulaw_search/pkulaw_verify`
与固定管线 `PkulawLegalClient`）的汇合点，一个埋点全覆盖。
失败/超限（抛 RuntimeError / BudgetExceededError）不记——与 F14 语义一致。

## 4. 数据模型（docker/init.sql）

```sql
-- 每次付费调用一行（LLM 每次调用 / Tavily 每次搜索 / pkulaw 每次 MCP 调用）
CREATE TABLE IF NOT EXISTS usage_logs (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
    day               DATE NOT NULL,                -- 冗余便于日聚合/分区
    user_id           TEXT NOT NULL DEFAULT 'default',  -- 预留分账
    request_id        TEXT,                          -- 关联 query_logs.request_id
    session_id        TEXT,
    source            TEXT NOT NULL,                 -- llm | tavily | pkulaw
    model             TEXT NOT NULL,                 -- deepseek-v4-flash | qwen2.5 | tavily-search | pkulaw-*
    tool              TEXT,                          -- pkulaw 的 purpose；tavily 的 depth
    backend           TEXT,                          -- deepseek | ollama | external
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    est               BOOLEAN NOT NULL DEFAULT FALSE,  -- True=估算值非真实 usage
    cost_cny          NUMERIC(12,6) NOT NULL DEFAULT 0, -- 金额快照（写入时按当时价格表算好）
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_logs_day   ON usage_logs (day);
CREATE INDEX IF NOT EXISTS idx_usage_logs_req   ON usage_logs (request_id);

-- 价格表（config 默认值首启灌入，前端可编辑）
CREATE TABLE IF NOT EXISTS pricing (
    key         TEXT PRIMARY KEY,      -- llm.deepseek.input_hit / tavily.credit_cny / pkulaw.point_cny ...
    value       NUMERIC(16,8) NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'cny',  -- cny | point | credit | token
    note        TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

存储层放 `src/observability/usage_store.py`：
- `record_usage(**row)`：每次调用一行，**失败 debug 级吞掉**（观测组件故障绝不拖垮主链路，沿用 query_logs 同款原则）；
- `read_usage_summary(days)` / `read_usage_detail(date, offset, limit)` / `read_usage_breakdown(days)`：纯 SQL 聚合，**不建 rollup**（D5）；
- `read_pricing()` / `upsert_pricing(key, value)`：价格表读写，内存缓存 + `updated_at` 比对，写后失效。

## 5. 价格表默认值（config）

| key | 默认值 | 依据 |
|---|---|---|
| llm.deepseek.input_hit_cny_per_m | 0.02 | DeepSeek 官方（2026-09-03） |
| llm.deepseek.input_miss_cny_per_m | 1 | 同上 |
| llm.deepseek.output_cny_per_m | 2 | 同上 |
| llm.ollama.* | 0 | 本地免费 |
| tavily.credit_cny | 0.008 USD→≈0.058 CNY | 官方 PAYG $0.008/credit；免费额度内实际不花钱，仅估算参考 |
| pkulaw.point_cny | ≈0.003 CNY/积分 | 官方体验档 ¥18/6000 积分；充得多单价更低（¥112/40000） |
| pkulaw.search.points_per_call | 125 | 语义检索类（search_article/search_case） |
| pkulaw.verify.points_per_call | 25 | 关键词/精确类（get_article/get_law_list）——按实际工具用途定 |
| pkulaw.recognition.points_per_call | 125 | 识别溯源/超链/幻觉修正类 |

北大法宝计价（官方 mcp.pkulaw.com 计价面板，2026-09-03 查证）：
- 新用户注册送 10,000 积分；部分平台连接器再送 10,000；有每日登录领取（当日有效）。
- 充值档：体验 ¥18/6,000 积分 ｜ 基础 ¥112/40,000 ｜ 进阶 ¥260/100,000 ｜ 高能 ¥520/200,000。
- 单次消耗：**基础关键词类 ≈25 积分，语义/识别/超链/幻觉修正类 ≈125 积分**。
- 用户当前余额/实际套餐在官方 console 可见，`pkulaw.point_cny` 与每次消耗积分值**以用户在前端改的为准**。

Tavily（docs.tavily.com）：
- 免费 1,000 credits/月（学生可申请更多）；basic search=1 credit、advanced=2 credits。
- PAYG $0.008/credit；月付套餐 $0.0075~0.005/credit。

## 6. API 设计（src/api/routes.py，均需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/usage/summary?days=7 | 按日聚合：{day, cost_cny, llm_calls, tavily_calls, pkulaw_calls, tokens_in, tokens_out, est_cost} |
| GET | /api/usage/detail?date=&offset=&limit= | 明细分页（含 est 标记、cache 拆分、tool） |
| GET | /api/usage/breakdown?days=7&group=source\|model\|tool | 归因聚合（饼图/条形数据源） |
| GET | /api/usage/pricing | 读价格表（含默认值合并展示） |
| PUT | /api/usage/pricing | 改价格表（body: {key, value}[]）→ 写 PG + 失效缓存 |
| GET | /api/budget | 已有，F14 当日次数状态，面板顶部复用 |

聚合口径统一走 `usage_store.read_usage_*`，**任何接口都不得直接查表拼 SQL**（避免口径漂移）。

## 7. 前端（Vue3 admin 计费页）

新增 `frontend/src/views/UsagePanel.vue` + router 项 + `api/usage.ts`。页面结构（线框已评审）：

1. **顶部 KPI 卡**：今日 LLM tokens（输入/输出拆 cache）｜今日估算费用 ¥（含免费额度标注）｜LLM 调用次数 x/y（F14 实时值）｜Tavily/法宝 剩余额度；
2. **近 7/30 日费用趋势**：柱状/折线，标注免费额度内区间；
3. **今日构成**：按 source/model 的金额占比（DeepSeek 真金白银 vs Ollama 免费 vs Tavily/法宝 积分额度）；
4. **最近调用明细**：时间/来源/模型/tool/tokens(或次数)/est 标记/费用，分页；
5. **价格设置抽屉**：`PUT /api/usage/pricing`，编辑即生效，带「恢复默认」按钮。

## 8. 测试计划

| 测试文件 | 覆盖 |
|---|---|
| tests/test_usage_callback.py | LLMUsageCallbackHandler：有 usage 记真实值；无 usage 走估算标 est；on_llm_error 不记；与预算 callback 并列挂载不冲突 |
| tests/test_usage_pricing.py | 计价器：cache 拆分的三种路径；价格表覆盖 config 默认；改价后新纪录用新价、旧纪录快照不变 |
| tests/test_usage_pkulaw.py | FakePkulawClient：_call 成功记一行（tool=search/verify）；失败/超限不记；Agent 工具与固定管线两条路径都覆盖 |
| tests/test_usage_api.py | summary/detail/breakdown/pricing 读写；鉴权；聚合口径与直接查表一致 |
| 回归 | 全量 pytest + ruff（CI 门禁） |

## 9. 落地顺序（每步独立 commit）

1. `docker/init.sql` 加 usage_logs / pricing 两表 + DDL 测试；
2. `config.py` 价格默认值 + `usage_store.py`（record/read/pricing，先不接埋点，单测通过）；
3. `usage_callback.py` + factory 挂载 + DeepSeek cache 拆分解析（测试红→绿）；
4. Tavily search() 埋点 + pkulaw `_call()` 埋点（两条路径单测）；
5. `/api/usage/*` 五个接口 + 鉴权（测试）；
6. 前端 UsagePanel 页 + 价格设置抽屉 + 构建门禁；
7. 更新 AGENTS.md / CHANGELOG.md（F15），全量回归后 commit。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 流式响应拿不到 usage_metadata | ChatOpenAI 开 `stream_usage=True`；仍无则 tiktoken 估算标 est |
| DeepSeek cache 字段随 SDK 版本漂移 | 三级降级解析 + 取不到按未命中保守计 + est 标记；价格表可调 |
| 埋点写 PG 影响主链路延迟 | 写入失败 debug 吞掉；量小直插可接受，量大可换批量队列（本期不做） |
| 价格表数据与官方刊例脱节 | 价格可前端改；默认值注明查证日期；历史快照不受改价影响 |
| pkulaw 工具实际积分消耗与默认不符 | 用户可在前端按官方 console 实测值覆盖（工具类型粒度） |
