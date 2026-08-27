# DECISIONS.md — 决策记录

> 记录"为什么选 A 而非 B"。修改相关模块前先查这里，**不要重新争论已定决策**；推翻旧决策时必须在本文留痕（新增条目说明替代关系）。

格式：`D-编号 | 状态 | 决策 | 备选与否决原因`

## 重构总纲（PRD 已确认，2026-08-12）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-R1 | 模型双后端：外接 API（DeepSeek）为主，Ollama 本地降级 | 纯本地 / 纯 API | 实时性需要强模型，降级保可用性（REQ-U1/UW2） |
| D-R2 | Agent 形态先工具调用（M1），预留多 Agent（M3） | 直接上多 Agent 规划 | 风险递进，先验证工具编排可靠性 |
| D-R3 | 通用搜索用 Tavily，Key 存宿主 `.env` 经 compose 注入 | Bing/Serper 等 | 价格与易用性；Key 后端统一管理 |
| D-R4 | 法律垂直源：官方免费库（国家法律法规数据库 + 人民法院案例库）为核心，小包公可选补充 | 只用第三方付费 API | 合规与成本；官方源权威性最高 |
| D-R5 | 双路冲突裁决：内部库优先；网络信息仅作线索，法律依据必须回源官方库二次验证 | 网络结果直接采信 | 幻觉与错误信息风险（REQ-U3/UW3/E4） |
| D-R6 | 部署：本地物理机/VM + Docker Compose，pgvector/Redis 容器化 + 宿主机持久化 | 云托管 K8s | 数据主权（《数据安全法》）、无 GPU 环境约束 |
| D-R7 | 预算熔断必须做：日上限 50 元，超阈值自动暂停（M3/F14） | 事后对账 | API 费用失控风险 |
| D-R8 | 前端过程透明化必须做：SSE 推送工具调用过程 | 只给最终答案 | 用户信任度（US-4，PRD P4） |

## M1 架构决策（2026-08-12/13，详见 `docs/M1-架构设计.md`）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M1-1 | **不用** langchain `bind_tools`，自研 `LLMBackend.chat_with_tools()` + LangGraph 手动 StateGraph ReAct | langchain 原生 AgentExecutor | 双后端（OpenAI 兼容 + Ollama）统一抽象、对 DeepSeek V4 parallel_tool_calls 行为可控 |
| D-M1-2 | 模型用 `deepseek-v4-flash`（2026-07-24 deepseek-chat 已弃用） | deepseek-chat | 官方弃用；v4-flash 非思考模式，function calling 完整支持 |
| D-M1-3 | `FailoverLLMBackend`：4xx（认证/权限）→ 降级 Ollama；429/5xx/408 → 走 retry 不降级 | 一律重试 / 一律降级 | 4xx 重试无意义；429/5xx 是暂时性故障 |
| D-M1-4 | Ollama 降级后**退化为固定管线**（不支持工具编排） | Ollama 也跑 ReAct | 小模型 tool calling 不可靠，固定管线保底质量 |
| D-M1-5 | `AGENT_REACT_ENABLED=false` 回退旧固定管线图（AC-7） | 只保留新路径 | 向后兼容与故障逃生通道 |
| D-M1-6 | 空 name 的占位 tool_call 一律过滤（agent_node + 双 backend `_parse_tool_calls`） | 当作未知工具回灌错误 | DeepSeek V4 想直接回答时的固有行为，回灌错误会引发空转（Bug 2939ab3） |
| D-M1-7 | 项目迁移：新代码只写 LexAgent，Law-RAG-Agent 保持干净上游 | 在上游继续改 | 用户明确要求上游可随时对照/复用 |

## M2 决策（2026-08-27）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M2-1 | 双路并行靠 LLM 的 parallel_tool_calls（一轮同时发 retrieve_knowledge + web_search），不做独立并行节点 | 图内固定并行扇出 | 保持 Agent 自主决策语义（PRD G1）；并行性由模型决策天然获得 |
| D-M2-2 | 融合在**最终 sources 组装阶段**做（`fusion.py`），不阻断 ReAct 循环 | 每轮工具结果即席融合 | 答案由 LLM 基于工具 summary 生成，融合价值在来源标注与冲突裁决（F10），即席融合增加循环复杂度 |
| D-M2-3 | 人民法院案例库无公开 API → 用 Tavily 域限定搜索（include_domains）发现案例线索，标注"官方域线索" | 爬虫采集 | PRD 风险清单 Q4 待定；域限定搜索零维护成本，合规 |
| D-M2-4 | 国家法律法规数据库走其公开 JSON 接口（flk.npc.gov.cn/api/search），失败时工具返回 ok=False 不阻断 | 失败自动回退 Tavily | 官方源不可用时应让 LLM 知道"验证不可用"，而非静默降级为未验证线索（REQ-UW1 语义） |
| D-M2-5 | 冲突裁决规则：web 结果提及内部库已有法名 → 标注 `web_unverified` 且内部条目排前；官方源命中 → `verified_official` | 复杂语义比对 | 法条级语义冲突检测需要 LLM 参与，M2 用来源级裁决满足 F8，语义级留给 validate 节点 |
