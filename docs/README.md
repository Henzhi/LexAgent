# docs/ — 文档索引

LexAgent 项目文档总目录。按主题分为六类，便于检索。

> **链接约定（重要）**：所有文档沿用 `docs/<文件名>` 的**根相对路径**写法（AGENTS.md / CHANGELOG.md / DECISIONS.md / README.md 及文档互链均如此引用）。**请勿随意把单文件移进子目录**，否则会破坏既有链接。本索引也仅做导航，不改文件位置。
>
> **姊妹仓库材料已移出**：原 `docs/docs/` 实为姊妹仓库 **Law-RAG-Agent** 的课程/汇报/简历面试材料（pptx、docx、md 等），与 LexAgent 无关，已整体迁至仓库根的 `archive/law-rag-agent/`（且被 `.gitignore` 忽略、不入库）。

项目总入口见 [`../AGENTS.md`](../AGENTS.md)。

---

## 一、产品与重构（PRD）

| 文件 | 说明 |
| :--- | :--- |
| [`自主Agent重构PRD.md`](自主Agent重构PRD.md) | 重构总需求，EARS 原则撰写，含验收标准 AC-1~AC-7。 |

## 二、里程碑交付（M1–M4）

| 文件 | 说明 |
| :--- | :--- |
| [`M1-架构设计.md`](M1-架构设计.md) | M1 详细设计，D1~D7 决策、共享约定 §8。 |
| [`M2联调结论-2026-08-28.md`](M2联调结论-2026-08-28.md) | M2 验收结论：双路径口径、配额策略、前端渲染约定（前端/测试必读）。 |
| [`M3-F12-人工确认技术方案.md`](M3-F12-人工确认技术方案.md) | F12 spike 结论：前置确认方案、checkpointer 选型、两种确认粒度成本对比、风险清单。 |
| [`M4-多Agent路线图.md`](M4-多Agent路线图.md) | 多 Agent 演进唯一权威路线：两条核心原则、Agent 三问、目标拓扑、阶段与退出判据（M4 必读）。 |

## 三、架构决策记录（ADR）

| 文件 | 说明 |
| :--- | :--- |
| [`adr-001-retrieval-config-alignment.md`](adr-001-retrieval-config-alignment.md) | 检索配置对齐决策。 |
| [`adr-002-remove-chapter-summary.md`](adr-002-remove-chapter-summary.md) | 移除章级摘要决策。 |
| [`adr-003-enterprise-upgrade-design.md`](adr-003-enterprise-upgrade-design.md) | 企业级升级设计（篇幅最大，约 70KB）。 |

## 四、设计图（Mermaid）

| 文件 | 说明 |
| :--- | :--- |
| [`class-diagram.mermaid`](class-diagram.mermaid) | 类图。 |
| [`sequence-diagram.mermaid`](sequence-diagram.mermaid) | 时序图（ReAct 主流程与降级流程）。 |

## 五、检索与质量评测（Evaluation）

| 文件 | 说明 |
| :--- | :--- |
| [`retrieval_eval.md`](retrieval_eval.md) | 检索评测方法与指标。 |
| [`retrieval_noise_fix.md`](retrieval_noise_fix.md) | 检索噪声修复。 |
| [`answer_quality.md`](answer_quality.md) | 回答质量评测（131 条，幻觉率观测值）。 |
| [`optimization-plan.md`](optimization-plan.md) | 检索优化计划。 |
| [`检索评测与优化报告-2026-08-03.md`](检索评测与优化报告-2026-08-03.md) | 条件 BM25 混合（RRF，w=3.0）优化结论。 |
| [`检索评测运行手册.md`](检索评测运行手册.md) | 评测运行操作手册（multi100 / colloq148 等基准）。 |
| [`向量路质量排查-2026-08-29.md`](向量路质量排查-2026-08-29.md) | 向量路质量排查实证（权重重定 w=3.0→0.5）。 |
| [`检索质量与响应性能评估-2026-08-31.md`](检索质量与响应性能评估-2026-08-31.md) | 质量与响应性能综合评估。 |

## 六、测试 / 审计 / 审查 / 报告（Reports）

| 文件 | 说明 |
| :--- | :--- |
| [`测试报告-2026-07-30.md`](测试报告-2026-07-30.md) | 早期测试报告（含短期重构待排期项）。 |
| [`审计报告-2026-07-30.md`](审计报告-2026-07-30.md) | 审计报告，重构待排期项来源。 |
| [`代码审查报告-2026-09-01.md`](代码审查报告-2026-09-01.md) | 代码审查报告。整改：2026-09-01 当日 7 commits（高危），09-02 再收尾 9 commits（B1–B8，见 CHANGELOG）。 |
| [`流式响应稳定性修复报告-2026-08-03.md`](流式响应稳定性修复报告-2026-08-03.md) | SSE 流式响应稳定性修复。 |
| [`smoke_test_report.md`](smoke_test_report.md) | 冒烟测试报告。 |
| [`technical_report.md`](technical_report.md) | 技术报告。 |
| [`unit_test_report.md`](unit_test_report.md) | 单元测试报告。 |
| [`常见错误清单.md`](常见错误清单.md) | 复发错误知识库（症状→根因→预防），**改代码前必扫**。 |
| [`B2-法名推断spike报告-2026-08-30.md`](B2-法名推断spike报告-2026-08-30.md) | 法名向量最近邻选型（质心法 vs 描述文本法，Recall 数据）。 |

---
