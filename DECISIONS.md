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
| D-M2-6 | ~~SSE 流式路径状态合并用 `_merge_stream_update`~~ **已被 D-M3-1 取代并删除** | 直接 `state.update()` | 旧实现整体替换 messages 导致 DeepSeek 400 并降级 Ollama；补丁只治标（手工复刻框架行为），根治见 D-M3-1 |
| D-M2-7 | 轮数上限强制作答时注入 system 引导消息 + 兜底清除 DSML 工具调用语法 | 仅移除 tools 参数 | DeepSeek 在无 tools 参数时仍会以纯文本输出 DSML 工具调用语法，不引导/不清除则答案是一堆标记 |
| D-M2-8 | flk.npc.gov.cn 接口改版后走 `/law-search/search/list`（POST JSON），不回退旧 `/api/search` | 兼容旧端点 | 旧端点返回 405（Method Not Allowed），官方已废弃；新端点参数名/响应结构全变（`searchContent`/`rows`/`sxx` 等） |

## M3 决策（2026-08-28）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M3-1 | SSE 流式路径走 **LangGraph 编译图执行**：新增 `_build_react_loop_graph()` 纯 ReAct 循环子图（入口 agent），`_stream_react` 用 `graph.stream(state, stream_mode=["updates","values"])` 驱动——updates 映射 SSE 事件、values 取终态；删除手写 `while` 循环与 `_merge_stream_update` | ① 保留手写循环 + 补丁合并；② 切 `create_react_agent` prebuilt | 手写循环绕开框架 reducer，是 D-M2-6 那个 400 Bug 的**根因**，补丁只治标。走编译图让 `add_messages` reducer 与条件边各司其职，Bug 从结构上不可能复发，代码更短。prebuilt 要求 `BaseChatModel` + LangChain Tool、需重写 LLM 层（D1 已否决），成本远高于收益 |
| D-M3-2 | 循环子图与完整管线图**并存**（`_react_loop_graph` 供 stream、`_graph` 供 ask），不强行统一 | 流式也走完整图（入口 intent） | 流式路径调图前已完成意图识别/FAQ 检查/记忆检索，且有 FAQ 命中提前 return 分支，走完整图会重复执行。两图共享同一套 `react` 节点字典（节点无状态、闭包注入依赖），无行为分叉风险 |
| D-M3-3 | 工具定义用自研 `@tool` 装饰器（`base.py`），从类型注解 + `Annotated` 元数据推导 schema | ① 保持 class + `build_spec()`；② 用 LangChain `@tool` | 装饰器让工具定义从 ~40 行 class 缩到 ~10 行函数，schema 与实现同源（改签名即改 schema，不会漂移）。**明确不用 LangChain `@tool`**：它产出的 LangChain Tool 对象只有 `BaseChatModel.bind_tools` 能消费，会倒逼重写 LLM 层（D1 已否决）。自研版零依赖、产出仍是与手写完全一致的 `ToolSpec`，下游 `ToolRegistry` / `chat_with_tools` 无需任何改动 |
