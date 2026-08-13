# ADR-001: 检索链路与生产配置对齐

## Status
Accepted — 2026-07-20

## Context
评测（`retrieval_noise_fix.md`、`eval_answer_quality.py`）已验证「章级摘要过滤 + Reranker 精排」
是当前最优检索配置，但存在配置/部署漂移：

1. 章级摘要（`chunk_type=chapter_summary`）噪声过滤只写在 `scripts/fill_eval_dataset.py`
   （评测脚本），运行时 `FAISSRetriever`/`RerankRetriever` 从不拦截 → 线上仍有 15-20% 查询
   召回 30+ 条无关章级摘要。
2. `config.py` 与 `docker-compose.yml` 默认 `RERANK_ENABLED=false`、`ADJACENT_ENABLED=false`、
   `AGENT_ENABLED=false`，与 README 宣称「最优配置 = 默认开启」矛盾 → 线上质量低于评测指标。
3. 意图识别写了两份（`engine.py` 正则 + `agents/graph.py` 关键词），行为不一致且需双份维护。

## Decision
- **改动 1**：新增 `RETRIEVAL_DROP_SUMMARY_CHUNKS=true`，在 `FAISSRetriever` 与 `PgvectorRetriever`
  的 `search`/`search_by_law` 中统一过滤 `chapter_summary`（FAISS 后过滤，PG 用 `WHERE <>`
  SQL 子句）。`fill_eval_dataset.py` 的内联过滤删除，改为单一事实来源。
- **改动 2**：`config.py` 默认 `RERANK_ENABLED=true`、`ADJACENT_ENABLED=true`
  （评测验证的质量提升，低延迟代价）；`docker-compose.yml` 同步 `RERANK_ENABLED=true`、
  `ADJACENT_ENABLED=true`。`AGENT_ENABLED` 保持默认 `false`：开启会额外发起 rewrite+validate
  两次 LLM 调用、延迟显著上升，且 0% 幻觉指标来自 reranker+过滤生成链路、不依赖 Agent。
- **改动 3**：新建 `src/rag/intent.py`，合并 `is_casual_query` / `classify_intent` /
  `needs_retrieval` 及共享词典常量；`engine.py` 与 `agents/graph.py` 改为从 `intent` 导入，
  删除各自重复实现。

## Consequences
- 变得更容易：线上检索质量立即对齐评测指标；章级摘要噪声在运行时根除；意图识别单一来源、
  后续改规则只改一处；README 与代码默认值一致。
- 变得 harder：reranker 在纯 CPU 环境会增加少量延迟（有 GPU 更佳）；`RETRIEVAL_DROP_SUMMARY_CHUNKS`
  为新增开关，若未来索引需把章级摘要当有效召回则需显式关闭。
- 风险：低。所有改动可经 `tests/test_classify.py`、`tests/test_rag_engine.py` 回归；
  过滤为纯追加、默认值翻转可逆（环境变量覆盖）。

## 涉及文件
- 新增：`src/rag/intent.py`
- 修改：`src/config.py`、`src/rag/retriever.py`、`src/rag/engine.py`、
  `src/agents/graph.py`、`scripts/fill_eval_dataset.py`、`tests/test_classify.py`、
  `docker-compose.yml`、`README.md`
