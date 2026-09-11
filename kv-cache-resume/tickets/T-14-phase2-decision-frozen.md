# T-14 · Phase 2 接入形态决策（⛔ 冻结）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⛔ 冻结 —— **解冻条件见下** |
| Epic | E2 · 接回 LexAgent（SPEC Phase 2） |
| 阻塞于 | T-13 |
| 阻塞 | Phase 2 全部实现、Phase 3 全部工作 |
| SPEC 依据 | §7 Phase 2、§11 Q4、R4、AGENTS.md 约定 8 |
| 预估 | 2~3 h（纯决策，不写接入代码） |

## 解冻条件

SPEC §7 明确写着「**需架构决策，暂不启动**」、R4「**决策前不动手**」。本票解冻需同时满足：

1. T-13 完成（策略层能力与验收结论都已落地）
2. SPEC §11 Q4 被正式提出讨论（自包 `BaseChatModel` 还是并列新后端）

**在解冻前不要写任何 `src/` 代码。**

## 要做出什么

一份 ADR，回答一个问题：**怎么把 `llama-server` 接回 LexAgent，而不破坏现有约定？** 并且给出可回滚方案。

## 已知冲突（本票的起点）

LexAgent 的 LLM 层走 LangChain `ChatOllama`（`/api/chat`），**拿不到 `/slots` API**。两条候选路径：

| 路径 | 说明 | 初步权衡 |
| :--- | :--- | :--- |
| **(a) 自包 `BaseChatModel`** | 直连 `llama-server`，保留 LangChain 生态互操作（`bind_tools`、callbacks） | 需要自己维护一层适配；但生态能力保留完整 |
| **(b) 并列新后端** | `llama-server` 作为新的降级后端，与 Ollama 并列 | 改动面小；但可能绕开 LangChain 抽象，与 D-M3-13 之后的约定不一致 |

## 范围

**做**

- 两条路径各出：**改动文件清单**（含 `src/llm/factory.py` / `failover.py` / 预算埋点）、风险、回滚步骤
- **强制约束清单**（缺一不可）：
  - 新后端必须挂 `callbacks=budget_callbacks()` —— 否则 F14 预算静默漏计。LexAgent 有 `test_callback_mounted_on_real_backends` 专门守这条，**要写清怎么用这个测试验证**
  - 必须带开关 + 一键回滚，且**默认关闭**
  - 不得改动 `ChatOllama` 现有的降级语义（AGENTS.md 约定 2：降级判定与自动回切规则）
- 影响面评估：与现有 `_react_enabled` 动态属性、failover 冷却窗口的关系
- 输出 ADR：`docs/adr-004-llama-server-接入形态.md`
- 决策留痕进 `DECISIONS.md`（新条目，编号按当时的日期规则）

**不做**

- **不写任何 `src/` 代码**（本票是决策票）
- 不实现 Phase 2 的接入（解冻并决策后另开票）
- 不碰 Phase 3

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `docs/adr-004-llama-server-接入形态.md` | 两路径对比 + 决策 + 回滚方案 |
| `DECISIONS.md` | 决策留痕条目 |

## 验收清单

- [ ] 两条路径各有「改动文件清单 + 风险 + **回滚步骤**」
- [ ] 明确 F14 预算不漏计的验证方法，**引用 `test_callback_mounted_on_real_backends`**
- [ ] 写明与 AGENTS.md 约定 2（降级判定 / 自动回切）的相容性，或说明冲突及处置
- [ ] 开关默认值 = 关闭，且回滚路径是「改配置」而不是「回滚代码」
- [ ] 决策留痕进 `DECISIONS.md`
- [ ] **不含任何 `src/` 改动**（可用 `git diff --stat src/` 自证）

## 备注

- 本票刻意写成「决策票」而不是「实现票」：R4 说得很清楚，架构冲突不解决就动手，做出来的东西大概率要推倒。
- `AGENT_REACT_ENABLED` 那边有个已经踩过的坑值得参考：**别把「是否降级」在构造期固化**（D-0902-3）。新后端的可用性判断也要是动态求值的。
- 如果最终选 (a)，注意 LexAgent 已经因为 D-M3-13 走过一次「从自研 backend 转向 LangChain 生态」的迁移——**不要再走反方向**，除非有明确理由并把理由写进 ADR。

## 完成后

解冻并进入 Phase 2 实现（另开 Epic），或按 DECISIONS 记录的方向调整。
