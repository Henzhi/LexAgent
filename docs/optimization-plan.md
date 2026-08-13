# 系统优化规划与落地记录

> 本文档记录 Law-RAG-Agent 的优化方向、落地进度与数据驱动决策依据。
> 最后更新:2026-08-04

## 一、优化总览

| 优先级 | 方向 | 状态 | 落地版本/提交 |
|---|---|---|---|
| 🔴 P0 | 可观测性(QueryLogger 接入) | ✅ 已落地 | v0.6,本次 |
| 🔴 P0 | 对话记忆写入链路打通 | ✅ 已落地 | save_memory 幂等接线 |
| 🟡 P1 | 上下文窗口 TokenBudget 接入 | ✅ 已落地 | 动态窗口 + 分段预算 |
| 🟡 P1 | FAQ 缓存迁移 Redis Stack | ✅ 已落地 | 向量检索 + 原生 TTL |
| 🟢 P2 | 记忆反思/聚类合并 | ⏸ 待数据验证 | 依赖 query_logs 命中率 |
| 🟢 P2 | 记忆混合检索(BM25+RRF) | ⏸ 待数据验证 | 依赖记忆命中率 |
| 🟢 P2 | 短期记忆摘要压缩 | ⏸ 待评估 | 对话超长时 LLM 压缩历史 |

## 二、本次落地:可观测性(2026-08-04)

### 2.1 问题

`QueryLogger`(v0.5)已实现但**零调用点**,`query_logs` 表长期为空,导致:
- FAQ 缓存命中率、记忆命中率等关键指标无法统计
- 各阶段耗时(意图/检索/生成)无数据,性能瓶颈靠猜
- 无法做数据驱动的调优决策(阈值、缓存策略)

### 2.2 改动

| 文件 | 改动 |
|---|---|
| `src/observability/query_log.py` | v0.6:**共享连接**(消除每次写入新建连接)+ 断线自动重连 + 线程锁 + `finalize` 幂等 + 新增 `start()`(生成器路径) |
| `src/agents/graph.py` | `ask()`/`stream()` 接入:记录 `intent/faq/memory/retrieve/generate/validate` 分阶段耗时与 FAQ 命中、记忆使用、检索数 |
| `src/rag/engine.py` | `ask/ask_stream/chat/chat_stream` 接入:记录意图、检索数、生成耗时 |
| `src/api/dependencies.py` | `get_query_logger()` 单例,注入 engine 与 agent;初始化失败降级为 `None`(不影响主流程) |
| `pyproject.toml` | 移除 `structlog` 死依赖(代码零引用) |
| `tests/test_query_log.py` | 新增 7 个测试:共享连接/自动兜底/幂等/手动模式/断线重连 |

### 2.3 记录内容

`query_logs` 表每条记录:

| 字段 | 含义 |
|---|---|
| `user_id` / `query` | 用户与问题 |
| `intent` | 意图分类(casual/law_lookup/case_query) |
| `retrieved_count` / `reranked_count` | 检索/重排数量 |
| `faq_cache_hit` | 是否 FAQ 缓存命中 |
| `memory_docs_used` | 使用的历史记忆条数 |
| `llm_tokens_used` | LLM token(当前为占位,后续可接 usage) |
| `total_latency_ms` | 总耗时 |
| `stage_timings` | 分阶段耗时 JSON |

### 2.4 数据使用方式

```sql
-- FAQ 缓存命中率
SELECT count(*) FILTER (WHERE faq_cache_hit) * 100.0 / count(*) AS hit_rate
FROM query_logs;

-- 高频问题 TOP10(缓存优化的数据依据)
SELECT query, count(*) FROM query_logs GROUP BY query ORDER BY count(*) DESC LIMIT 10;

-- 平均分阶段耗时
SELECT round(avg(total_latency_ms)) AS avg_total,
       round(avg((stage_timings->>'retrieve')::float)) AS avg_retrieve,
       round(avg((stage_timings->>'generate')::float)) AS avg_generate
FROM query_logs;

-- 记忆命中率(判断记忆反思/合并是否值得做)
SELECT count(*) FILTER (WHERE memory_docs_used > 0) * 100.0 / count(*) AS memory_usage_rate
FROM query_logs;
```

## 三、前期已落地优化回顾

### 3.1 记忆系统(已落地)

- **写入链路打通**:会话保存(≥6 轮)异步固化,`save_memory` 幂等 UPSERT(同会话覆盖巩固)
- **重要度预筛**:按轮数 6/10/15 分档 0.6/0.8/1.0,避免噪音记忆
- **检索加权**:`score = relevance × importance_norm × decay(t)`,指数时间衰减(半衰期 7 天)
- **过期清理**:`clean_expired()` + 后台循环失败重试(不再永久失效)

### 3.2 上下文窗口(已落地)

- **TokenBudget 接入生产**:`build_budgeted_prompt()` 动态使用模型真实窗口,分段预算(system/记忆+条文/历史),对比查询动态放大检索预算
- **Ollama num_ctx**:显式下发请求上下文窗口(`OLLAMA_NUM_CTX` 可覆盖),避免服务端 2048 默认静默截断
- **模型窗口映射补齐**:新增 `qwen2.5:3b → 32000`(真实 32768 取保守值),修复"小模型未映射 → 浪费 ~4K 上下文"问题
- **历史预算筛选**:按 chat_history 段预算从后往前选,替代固定"6 轮×300 字"

### 3.3 FAQ 缓存 Redis Stack(已落地)

- `FAQCacheRedis`:HNSW 向量检索(1024 维)+ 原生 TTL + 命中续期 + Set 级联失效
- **bug 修复**:`doc.id` 双重前缀(命中不刷新 TTL);失效索引 Set 与 FAQ 同生命周期(消除僵尸引用)
- `FAQ_CACHE_BACKEND=redis|pg` 开关,pg 为无 Redis 回退

## 四、待办优化(数据驱动决策)

### 4.1 记忆反思/聚类合并(P2)

**前置条件**:`query_logs` 中 `memory_usage_rate` 明显偏低(如 < 30%)且用户抱怨"记混/记不全"。
**思路**:离线 batch 对 `conversation_memories` 做冲突检测与聚类,把重复案件摘要合并为实体百科。
**风险**:法律咨询场景跨会话重复少,过度合并会混淆不同案件细节 → 需谨慎。

### 4.2 记忆混合检索(P2)

**前置条件**:记忆命中率低,且 Top-3 纯向量召回不足。
**思路**:复用 RAG 链路的 BM25+RRF,对摘要做混合召回。

### 4.3 短期记忆摘要压缩(P2)

**前置条件**:长会话中 TokenBudget 截断历史导致上下文丢失。
**思路**:历史超预算时,用 LLM 把旧对话压缩为摘要(上下文缩减策略的"摘要压缩"档)。

### 4.4 其他工程项

- **CI/CD**(GitHub Actions:lint + pytest)
- **评测基准**:`evaluation/` 脚本配合 `query_logs` 做回归基准
- **LLM token 统计**:接 LLM 返回的 usage,补全 `llm_tokens_used`
