# ADR-002: 移除章级摘要（chapter_summary）检索噪声

## Status
Accepted

## Context
架构评审发现 `src/chunking/chunker.py` 的 `_chunk_chapter_summaries` 为每章生成一个
`chapter_summary` chunk：内容是**该章所有条文各取前 100 字拼接的 blob**，元数据
`article_range` 却谎称覆盖整章。这些 chunk 与 `article` chunk 一起灌入同一 FAISS 索引
（`scripts/build_index.py:99`），占 3753 个向量的 8.1%（304 个）。

实证（task `cnWb4K`，已排除 summary 的纯法条级检索）：
- **纯向量 Recall@5 = 0.72**（真值）
- 混合 BM25 全为负优化（0.26~0.56）
- README 宣称的 Recall@5=0.80 是在 summary 参与检索、且假元数据制造假阳性的前提下算出的
  → 含约 8 个百分点水分

用户追问"保留章级索引效果会更好吗"：现状（broken blob + 假元数据）只抬高纸面数字、
损害答案精度，**并不更好**；重建为独立索引 + 真摘要（方案 C）对跨章主题查询可能真优于 0.72，
但需自建自测且引入复杂度。本项目以精确条文问答为主，章级索引边际收益小。

## Decision
采用**方案 A**：从索引中移除 chapter_summary chunk。

1. 翻转 `scripts/build_index.py` 默认行为：
   `add_chapter_summary=not args.no_summary` → `add_chapter_summary=args.with_summary`
   （默认 **False**，即默认不含 summary）；新增 `--with-summary` 作为可逆开关。
2. 用新默认重建索引：`uv run python scripts/build_index.py build`。
3. `RETRIEVAL_DROP_SUMMARY_CHUNKS`（ADR-001 引入的运行时过滤）在索引已无 summary 后
   退化为无害的防御性 no-op，保留以维持 `--with-summary` 路径的安全。
4. 重建前已备份原索引：`data/vector_store/law_index_bge.bak_before_nosummary`。

## Consequences
- **更易**：Recall 指标变诚实（0.72 真值）；索引更小（3449 vs 3753）；检索结果不再含
  30+ 条无关条文碎片；跨条上下文已由接好的 `AdjacentExpander` 提供。
- **更难**：失去"章级粗粒度信号"，对跨章/主题级查询的覆盖减弱
  （由 AdjacentExpander 部分弥补；若后续评测证明主题级查询显著掉点，可重访方案 C：
   独立 summary 索引 + 真摘要 + 路由兜底）。
- **可逆**：原索引已备份；`--with-summary` 可一键恢复含 summary 的构建。
