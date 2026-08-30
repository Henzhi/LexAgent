# M4 多 Agent 路线图 — 生长式演进（2026-08-30 立项）

> 决策号 D-M4-1（见 `DECISIONS.md`）。本文档是多 Agent 演进的**唯一权威路线**：
> 讨论结论、拓扑判定、阶段划分与退出判据。动多 Agent 相关代码前先读这里。

## 0. 背景与触发

D-M3-11（F13）当时结论是「只保留 `sub_agent` 字段，不建规划节点」，并写明重新立项
触发条件。2026-08-30 产品明确方向：**多 Agent 多场景、意图识别中的规划是长期方向**
——触发条件「出现第二个 Agent 角色」正式满足，立项为 M4。

## 1. 两条核心原则（所有阶段都必须遵守）

### 原则一：按场景拆出口，不按工具拆内脏

主 Agent 现在的核心智能是**跨源迭代验证**（看到网络线索 → 决定回官方源核验；
内部库没覆盖 → 换关键词再检索），这是 ReAct 循环存在的意义（D-M2-1）。

- ❌ **不做**：把检索/网络搜索/官方核验拆成子 Agent——每个只调一个工具的
  "Agent"是给函数开会，白付一跳 LLM 路由的延迟与预算，跨源迭代还得在
  orchestrator 层重建同一个循环；
- ✅ **做**：检索、搜索、核验**保持为工具**；拆出去的是**重流程场景**
  （类案报告、文书生成、审核）——它们有固定流程形状或独立输出契约。

预算依据：实测一次复杂查询 18~20 次 LLM 调用，日预算 5000 ≈ 250 次查询。
朴素 orchestrator（每查询 +3~5 次路由调用）会砍掉 15~20% 容量——
**A 类路径必须保持零编排**，orchestration 开销只让 B 类场景付。

### 原则二：生长式迁移，不做重写式迁移

- 新 Agent 用 LangChain/LangGraph 生态标准件**长出来**（子图、标准 ChatModel、
  BaseTool 绑定）；开放循环型子 Agent 可用 `create_react_agent`；
- 主 Agent 的自有循环**不重写**（轮数上限强制作答 REQ-UW4、空 tool_call 过滤
  D-M1-6、重试/降级 D-M3-14 都长在它身上）；
- ❌ 永不立项：全盘 LangChain 风格化（消息重写 / ToolResult 换裸值 / 主循环换
  prebuilt）。风格化 = 新代码默认生态写法 + 搭车机会，不是改造运动。

## 2. Agent 判定规则（三问）

一个能力升级为独立 Agent 前过三问：

1. 需要**自主决定多步**吗（LLM 在循环里自己决定下一步）？
2. 需要**独立的确认契约**吗（开工前等人拍板）？
3. 需要**独立的输出契约**吗（结构化判定，不是一段文字）？

- 三问全否 → **工具**（挂主 Agent 工具表）；
- 固定流程 → **管线子图**（如文书生成：要素→检索→起草→引用核验）；
- 有自主判断与独立契约 → **真 Agent**（如审核：通过/驳回+修改意见）。

已判定的例子：北大法宝搜索 = 工具（已是）；法律文书生成 = 管线子图；
审核 = 真 Agent（`validate` 节点 + `HallucinationGuard` 是它的胚胎）。

## 3. 目标拓扑

```
intent + scene + plan（阶段 1 的规划对象）
        │
        ├─ A 类 / 未命中 ──→ 主 Agent（现有 ReAct 循环，原样保留）
        │                     工具表不变：retrieve / web / legal_source / pkulaw
        │
        └─ B 类重流程 ──→ 子 Agent 注册表（dict[str, 编译子图]，同 ToolRegistry 惯例）
                            ├─ similar_case_report → 类案子图
                            ├─ legal_document      → 文书子图
                            └─ contract_review 等  → 审查子图
                                    （每个重流程子 Agent 出口挂审核子图）
```

- **通信靠 state 字段**：子图把产物写进约定字段（`draft` / `report` /
  `review.verdict`），不存在 Agent 互发消息；控制权移交用 `Command(goto=...)`；
- **并行靠 `Send`**（将来"三场景同时初检"类需求，LangGraph 原生）；
- **主 Agent 双角色**：A 类执行者（不变）+ 子 Agent 失败/plan 不确定时的兜底
  （REQ-UW1「子 Agent 失败不阻断」的落点）；
- **共享底座全复用**：`chat_with_tools()` 入口（重试/降级/预算一次修好处处生效）、
  ToolRegistry、SSE 协议（加 `agent` 字段）、F12 确认流、fuse_evidence 出口融合。

## 4. 阶段划分与退出判据

### 阶段 0：M3 收尾（多 Agent 的 UX 底座）——已在 M3 任务清单
- F12 v1 一次确认（路径 A）；
- D-M3-12 SSE 断线重连。
- 流程更长更贵的多 Agent，「等人确认再跑」和「断了能续」是前置卫生条件。

### 阶段 1：意图识别升级为规划（plan 对象）
- `classify_scene` 从「打标签」升级为「出计划」：结构化 plan（场景 → 工具白名单
  → 约束 → 确认要求），交给现有单 Agent 消费；
- `agent_node` 按 plan 白名单过滤 schemas——`SCENES.tools` 字段首次被消费；
- **搭车**：`bind_tools` 从 OpenAI dict 切到 BaseTool 对象（`langchain_tools()`
  就位），白拿参数校验；
- B 类确认载荷直接用 plan 对象（F12 确认单 = plan 的雏形）；
- **退出判据**：A 类行为与现状逐字一致（回归守卫）；B 类确认单携带场景工具白名单。

### 阶段 2：第一个真子 Agent
- **审核子图先行**（替换 `validate` 节点：规则守卫 + LLM 审核 + `pkulaw_verify`
  逐条回源核验 → 通过/驳回+意见）——它同时是答案质量守门员；
- **类案报告子图**（第一个被路由的子 Agent）：检索类案 → 法宝核验 → 成报告，
  与 F12 确认流在同一入口汇合，验证「plan → 确认 → 分发」最小闭环；
- 预埋：`tool_log` / SSE 事件加 `agent` 维度（0.5 天，先于本阶段）；
- **退出判据**：答案质量基线（`eval_answer_quality`）对比，证明优于单 Agent；
  SSE 能区分"谁在说话"。

### 阶段 3：定型 supervisor 模式（2+ 子 Agent 验证后才做）
- 状态作用域化（子图 input/output schema 隔离，现在 AgentState 全扁平共享）；
- per-agent 预算记账（cost_budget 加 agent 维度）；
- checkpointer 正式引入（PostgresSaver）——多步计划跨轮恢复绕不开；
- 开放循环型子 Agent 默认 `create_react_agent` 标准写法；
- **退出判据**：新增一个场景子 Agent 的边际成本 ≤ 写一张小图（底座零改动）。

## 5. 立项前置与验收纪律

1. **`eval_answer_quality` 基线必须先于阶段 2 存在**——没有它永远无法证明
   "多 Agent 比单 Agent 好"；
2. 每个子 Agent 上线前跑一次基线对比；
3. 检索质量线（法名推断等）与多 Agent 线**并行推进**——多 Agent 解决编排，
   不解决检索（无 法名口语查询 6.7% 命中的问题与架构无关）。

## 6. 已否决项（勿重新争论）

| 否决项 | 理由 |
| :--- | :--- |
| 全盘 LangChain 风格化 | 见原则二；生态收益已在 D-M3-13 兑现，剩余自有抽象承载产品语义 |
| 按工具拆子 Agent（检索 Agent/搜索 Agent…） | 见原则一；跨源迭代是主 Agent 的核心智能 |
| 大爆炸重写主 Agent | D-M3-14 实证：迁移最易伤横切语义，且无新增收益 |
| 现在就引 checkpointer / PostgresSaver | 阶段 0/1 不需要（D-M3-9a/D-M3-12 已论证），阶段 3 再引 |
