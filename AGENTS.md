# AGENTS.md — 项目说明书

> 所有 AI Agent 的首要入口。修改代码前必读；与实际不符时以代码为准并回头更新本文档。

## 项目概述

LexAgent 是一套**法律 RAG 智能问答系统**，正从固定管线 RAG 重构为**工具调用型自主 Agent**（详见 `docs/自主Agent重构PRD.md`）。

- 里程碑：M1 工具调用型 Agent（已完成）→ M2 双路融合（已完成，2026-08-28）→ **M3 分场景人工确认（已完成，2026-08-30：F14/F11/F12/F13 全部收尾）** → M4 多 Agent 演进（**已立项 D-M4-1**，路线见 `docs/M4-多Agent路线图.md`，M4 代码未启动）
- 双 LLM 后端：外接 API（DeepSeek，OpenAI 兼容）为主，Ollama 本地为降级
- 双路检索：内部 pgvector 知识库（最高优先级法律依据）+ 网络搜索（Tavily，仅作线索）+ 官方法律源二次验证
- 姊妹仓库 `Law-RAG-Agent` 为干净上游，**所有新代码只写在 LexAgent**

## 技术栈

| 层 | 技术 |
| :--- | :--- |
| 语言 | Python 3.12+（uv 管理依赖），前端 Vue3 + Vite |
| API | FastAPI + SSE 流式（`/api/chat`、`/api/chat/stream` 免认证；conversations/knowledge/crawl/budget/usage 需 Bearer） |
| 编排 | LangGraph 1.2 StateGraph（手动 ReAct）；**D-M3-13 起 LLM 层与工具层走 LangChain 标准生态**：`BaseChatModel` + `bind_tools` |
| LLM | 自研 `LLMBackend.chat_with_tools()`；DeepSeek `deepseek-v4-flash`（主）+ Ollama qwen2.5（降级） |
| 检索 | pgvector(halfvec+HNSW) + BM25 条件混合（RRF）+ bge-reranker 精排 + 相邻扩展 |
| 存储 | PostgreSQL/pgvector + Redis（FAQ 语义缓存） |
| 搜索 | Tavily（通用）；官方法律源（国家法律法规数据库 flk.npc.gov.cn、人民法院案例库 anli.court.gov.cn、**北大法宝 MCP** pkulaw.com，M3+ / F9 扩展） |

## 目录结构

```
src/
├── agents/          # LangGraph 编排：graph.py（图）、react_nodes.py（ReAct）、nodes.py（固定管线）、state.py、prompts.py
│   └── tools/       # 工具层：base.py（ToolSpec/ToolResult）、registry.py、retrieve_knowledge.py、web_search.py
├── llm/             # LLM 后端：factory.py、openai_backend.py、ollama_backend.py、failover.py、retry.py、budget_callback.py（F14）、usage_callback.py（F15）
├── search/          # 外部搜索：tavily.py、legal_sources.py（M2）、fusion.py（M2）
├── rag/             # 检索链：retriever.py、engine.py、intent.py、scenes.py（M3/F11 场景分类）
├── api/             # FastAPI 路由
├── memory/          # 会话记忆 + hallucination_guard.py 幻觉守卫
├── observability/   # 可观测：query_log.py 查询追踪、cost_budget.py（F14 次数熔断）、usage_store.py（F15 用量计费存储/计价/价格表）
└── config.py        # 全部配置（.env 加载）；PRICING_DEFAULTS（F15 价格默认值）
tests/               # pytest（FakeRetriever/FakeToolLLM，不依赖外部服务）
docs/                # PRD、架构设计、ADR、评测报告
```

## 常用命令

```bash
uv sync                                     # 安装依赖
uv run pytest tests/ -x -q                  # 全量测试（conftest 已自动清空 TAVILY_API_KEY，2026-09-03）
uv run uvicorn src.api.main:app --reload    # 启动后端
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/   # lint + 格式门禁（CI 同款，提交前必跑）
docker compose up -d                        # pgvector / redis（本机已有旧容器 lawrag-db/lawrag-redis 时直接复用）
```

## 关键架构约定（改代码前必读）

1. **ReAct 循环走 LangGraph 图执行（D-M3-1）**：`agent_node` 调 `chat_with_tools` 决策 → `tools_node` 执行全部 tool_calls（并行）→ 回灌 → 循环；轮数上限 `AGENT_MAX_TOOL_TURNS=5`，达上限移除 tools 强制作答（REQ-UW4）。
   - **两条路径都走编译图**，禁止手写 `while` 循环步进节点：`ask()` 用 `_graph`（完整管线，入口 intent），`stream()` 用 `_react_loop_graph`（纯循环子图，入口 agent，D-M3-2）。⚠️ `_graph` 是**动态属性**（D-0902-3）：ReAct 可用时返回含 agent/tools 的图，降级期间回落 `_fixed_graph`（固定管线）——别在构造期缓存 `_graph`。
   - 消息累积由 `AgentState.messages` 的 `Annotated[list, add_messages]` reducer 保证，循环终止由条件边 `route_after_agent` 保证。**手工合并状态（如 `dict.update()` 覆盖 messages）会破坏 tool 消息与 `assistant(tool_calls)` 的配对关系，导致 DeepSeek 400 并降级 Ollama**（历史 Bug，勿重蹈）。
2. **回退路径（动态，D-0902-3）**：`AGENT_REACT_ENABLED=false` 或 **当前**主后端降级（Ollama）或 LLM 不支持工具 → 回退固定管线图（AC-7 向后兼容）。`_react_enabled` 是动态属性：降级实时回落固定管线、failover 冷却探测回切后**同一实例自动拿回 ReAct 能力**，不许把「是否降级」在构造期固化。
   - **Failover 降级判定（D-M1-3 + D-0902-1）**：4xx（认证/业务，408/429 除外）→ 降级；429/5xx 走 `retry.py` 重试**不降级**；重试耗尽抛 `LLMRetryExhaustedError`（哨兵，携带最后一次失败的状态码）→ **也降级**——持续 429/5xx 说明主后端当前不可用，有 Ollama 兜底就该用上。裸 `RuntimeError`（编程错误）仍不降级，别把内部 bug 误判成后端故障。
   - **自动回切（D-0902-2）**：降级后进入冷却窗口（默认 300s，`recovery_cooldown_seconds`=0 禁用回切保持旧语义）；冷却结束后**下一次真实请求兼作健康探测**——成功自动回切、失败继续降级并刷新冷却。⚠️ 不要为探测单独发 ping（白白消耗一次真实 Token + RTT）。
3. **工具失败不抛异常**：统一返回 `ToolResult(ok=False)`，summary 首词为错误标签（如"搜索不可用"），ReAct 循环继续。
4. **来源优先级与融合（D-M3-4 / D-M3-5）**：内部库 `internal_kb` > 官方源 `legal_source` > 网络 `web`。网络结果仅作线索，作为法律依据必须回源官方库二次验证。
   - 三路证据由 `search/fusion.py::fuse_evidence()` 融合：去重 → 来源加权排序 → 打 `verification` 状态（`verified_internal` / `verified_official` / `third_party` / `web_unverified`）。
   - **截断有保底配额**：网络权重最低（0.5×tavily_score），纯按分排序会被权威来源挤空，故 `_truncate_with_web_quota` 给网络留 `FUSION_WEB_MIN_SLOTS`（默认 2，设 0 关闭）——否则 Tavily 调用了用户却一条线索都看不到。
   - **流式与非流式口径必须一致**：两条路径都取 `fused_sources`，都不能直接返回原始 `retrieved_docs`（历史 Bug：非流式曾返回 74 条无 verification 的未去重文档）。新增来源字段时，`_dicts_to_retrieved` 的 `_SOURCE_TRACE_KEYS` 要同步加，否则字段在中间转换时被丢掉。
5. **summary 截断**：工具结果 summary ≤300 字符（`TOOL_RESULT_SUMMARY_MAX_CHARS`），防上下文膨胀。
6. **空 tool_call 过滤**：DeepSeek V4 想直接回答时会返回 name="" 的占位 tool_call，`agent_node` 必须过滤（历史 Bug 2939ab3）。
7. **模型名**：deepseek-chat 已于 2026-07-24 弃用，用 `deepseek-v4-flash`。
8. **预算熔断（F14，D-M3-6/7/8）**：外部付费 API 的日用量统计与熔断（`src/observability/cost_budget.py`）。
   - **埋点位置（D-M3-13 后已改）**：LLM 由 `LLMBudgetCallbackHandler`（`src/llm/budget_callback.py`）在 LangChain 调用链路上 check + record，**构造 ChatModel 时必须挂 `callbacks=budget_callbacks()`**——上层可直接 `chat_model.invoke()` 绕过 `LLMBackend` 公开入口，挂了 callback 才不会漏计（测试 `test_callback_mounted_on_real_backends` 守着这条）。Tavily 在 `search()` 内埋点。新增付费外部依赖时同步接入预算（新增 kind）。
   - **两级熔断**：LLM 超限整体熔断（API 前置拦截 `_budget_block_message()`，流式/非流式都返回友好提示）；Tavily 超限只降级该工具（`ok=False`，summary 首词「搜索额度已用尽」），回答照常生成。
   - **存储降级**：Redis 优先、不可用时退化进程内计数；统计异常一律告警放行，**统计故障不许拖垮主链路**。
   - 配置：`BUDGET_*`（阈值设 0 = 不限制，`BUDGET_ENFORCE=false` 只告警不拦截）；运维接口 `GET /api/budget`（需登录）。实测一次复杂查询约 18~20 次 LLM 调用，调整默认阈值时以此为参考。
9. **场景分类与人工确认（F11/F12，D-M3-9 / D-M3-10）**：
   - **场景清单是数据、分类逻辑是代码**：`src/rag/scenes.py` 的 `SCENES` 元组即清单（id/名称/A\|B/关键词/工具），`classify_scene()` 是逻辑。产品调整场景只改 `SCENES`，**不动任何函数**。打分用三级权重（正则 3.0 > 强特征词 2.0 > 普通关键词 1.0）。⚠️ **B 类场景禁止把裸通用词放进普通关键词**（D-0903-6）：「合同/协议/条款」遍布普通法律咨询（劳动合同/租赁合同/合同纠纷…），裸词 1.0 会让海量普通问答误进 B 类确认流程（历史 Bug 复发两次）——合同起草/审查只由动作强特征词（起草/审查/审核/审阅…）触发；三级权重用于 A 类场景之间与「第X条」正则压过通用词。
   - **分类在进图之前完成**：`ask()` / `stream()` 在意图识别后调用 `classify_scene()`，结果写入 `scene_id` / `scene_kind` / `scene_matched`。**不新增图节点、不改图结构**。
   - **F12 v1 已实现（2026-08-30）——确认点同样在进图之前**：B 类且未确认 → `ask()`/`stream()` 在场景分类后产出 `confirmation_required`（载荷含 scene/scene_name/prompt/options/confirm_id）并结束流，**零 LLM 消耗**；确认标记存 `src/memory/confirmation_store.py` 的 `ConfirmationStore`（Redis `SETEX` key=`lexagent:confirm:{user}:{session}`，value=已确认 query 防换题 R7，TTL `CONFIRMATION_TTL_SECONDS` 默认 600s=Q7 决策）；Redis 不可用退化进程内、**读取异常 fail-open 回落 A 类**（确认机制故障不阻断主链路）。新接口 `POST /api/chat/confirm`（校验仅 B 类场景 id；approved=False 清标记返回 JSON）。**2026-09-03（D-0903-7）确认后同连接直接续跑**：`approved=True` 写标记后即返回 SSE 事件流（与 `/chat/stream` 共用 `_build_stream_response`，断线重连/取消/归属登记语义一致），前端 `confirmSceneStream` 消费，无需再发一次 `/chat/stream`——标记仍写，旧客户端重发 stream 依旧兼容。**v1 不接 `interrupt()`、不加 checkpointer、不改图**（理由见 `docs/M3-F12-人工确认技术方案.md`）。测试 `tests/test_f12_confirmation.py`。
   - ⚠️ 若将来要上「逐步骤确认」（v2，需在循环内中断），**必须先读该文档的风险 R1**：无 checkpointer 时 `interrupt()` **不报错**，图静默停住、答案为空，前端永远等不到结果而后端日志无任何异常。必须在图构建处加自检断言。
   - 未命中场景时**保守回落 A 类**（`matched=False`），绝不因分类失败阻断回答。
10. **LangChain 标准生态（D-M3-13）**：
   - LLM 层内部用 `BaseChatModel`（`ChatOpenAI` / `ChatOllama`），经 `.chat_model` 暴露。⚠️ `.model` **仍是模型名字符串**（历史字段，18 处调用点在读），两者别混淆。
   - **多轮决策调用必须走 `llm.chat_with_tools()` 公开入口**（D-M3-14）：重试（D-M1-3）与 Failover 4xx 降级都实现于该入口链路，直接 `chat_model.bind_tools().invoke()` 会同时绕过两层——D-M3-13 迁移时踩过（瞬时 429/5xx 一次抖动整轮失败、主后端 4xx 不再降级 Ollama），已由 `TestAgentNodeCallSemantics` 守回归。`chat_model` 保留给标准生态互操作（挂 callback、运维脚本直调）。
   - **新增 LLM 后端必须挂 `callbacks=budget_callbacks()`**：漏挂不会报错，只是预算不再计数，熔断形同虚设（测试 `test_callback_mounted_on_real_backends` 守着）。
   - **重试仍用自研的 `is_retryable` + `wait_and_log`**（D-M1-3），实现于后端 `_chat_with_tools_impl` 等入口内部（LangChain 调用外层）；**不要**改用 `ChatOpenAI(max_retries=)`——它的判定标准与 D-M1-3（4xx 不重试交由 Failover 降级、429/5xx 重试）不一致。重试**耗尽**统一抛 `LLMRetryExhaustedError`（`src/llm/retry.py`），**不要再改回裸 `RuntimeError`**——丢状态码会让 failover 判定「非 4xx」不降级（历史 Bug，D-0902-1）。
   - 消息转换统一走 `src/llm/base.py` 的 `to_langchain_messages()` / `tool_calls_from_langchain()`；后者已内置 D-M1-6 的空 name 过滤。`Message` 数据类也在 `base.py`（D-0902-6 自 client.py 迁入），别再新建消息类。
   - LangChain 的 tool_calls 参数是**已解析的 dict**（不像 OpenAI 原始响应是 JSON 字符串），因此不存在 `parse_error`，工具的容错改为「参数校验失败」路径。
   - **工具执行前强制 pydantic 校验（D-0902-5）**：`ToolRegistry.execute()` 对带 `langchain_tool` 的工具先经 `tool_call_schema`（与发模型的同一份约束）`model_validate` 再调 executor——LLM 生成的参数是**不可信输入**，schema 约束必须在运行时不打折扣（非法枚举/错类型 → `ok=False`「参数校验失败」，幻觉参数白名单丢弃）。⚠️ 校验用 schema 后**仍走 `spec.executor()`**，不要改走 `langchain_tool.invoke()`——`BaseTool.run` 会把 ToolResult 拍平成字符串、破坏结构化结果契约。

11. **北大法宝 MCP 官方法律源（M3+ / F9 扩展，决策 D-PKULAW）**：接入 pkulaw.com 高权威源（法条原文 + 类案全文 + 核验 + 超链），优先级与现有官方源同级（`verified_official`）。
   - **懒加载 `mcp` SDK**：`src/search/pkulaw_mcp.py` 仅在真正调用时才 `import mcp`，未安装不影响模块导入与单测（单测一律用 `tests/fakes.FakePkulawClient`）。
   - **运行时按用途解析工具名**：pkulaw 聚合端点（默认 `mcp-law-agg`）把 10 个工具挂在一个 URL 下、名字带服务前缀且会变；客户端 `tools/list` 后按「用途关键词」匹配 name+description 建 purpose→name 映射，不硬编码工具名（SKILL 同款原则）。
     - ⚠️ **`_discover` 是协程，调用处必须 `await`**（历史 Bug）：漏 `await` **不报错**，只是 `_tool_map` 永远为空、静默退化为 `_FALLBACK_TOOL_NAMES`。而真端点实际工具名是**点分隔**（`mcp-law-search-service.search_article`），兜底快照是**下划线分隔**，一退化则**所有真实调用全部失败**且单测（Fake 绕过 `_a_call`）抓不到。回归测试见 `tests/test_pkulaw.py::TestPkulawToolDiscovery`。
     - 真端点已联调确认：10 个工具、8 个用途全部运行时命中零兜底；另有 `mcp-case.get_case_list`、`mcp-fatiao.get_law_item_content` 两个暂未映射用途，需要时再加进 `_PURPOSE_KEYWORDS`。
   - **参数平铺 + 结果按语义提取**：北大法宝工具 inputSchema 常声明包装体但后端只认平铺，一律传平铺；返回体形态不统一（裸数组/包裹体 `Data`/纯字符串），按字段语义而非名字取值，并清理链接锚点 `.0` 坏后缀。
   - **两条接入路径**：① 后端源——`PkulawLegalClient` 注册进 `LegalSourceClient` 门面，`legal_source_search` 自动融合（与既有国家库/案例库/小包公并列）；② ReAct 工具——`pkulaw_search`（检索）/ `pkulaw_verify`（核验+加链）按 `PKULAW_ENABLED` 与客户端可用性注册。
   - **预算熔断**：北大法宝按积分计费，新增 `KIND_PKULAW`（kind=`pkulaw`，`BUDGET_MAX_PKULAW_CALLS_PER_DAY` 默认 200），每次成功调用 `cost_budget` 先 check 后 record；超限工具层返回「法宝额度已用尽」、不阻断主链路（与 Tavily 同级降级语义）。
   - **配置只在 `.env`**：`PKULAW_MCP_URL` / `PKULAW_MCP_TOKEN`（聚合端点 Bearer），**严禁入库**（`.env` 已 gitignore）。

12. **用量计费面板（F15，旁路观测）**：在 F14「次数熔断」之外新增一套 **token/金额观测层**，回答"每天烧了多少钱、花在哪"。方案 `docs/F15-日志与Token计费面板-技术方案.md`。
    - **架构：旁路，不动 F14 熔断主链路**。熔断继续按次数（实时、原子、便宜），面板展示层按 token/金额。改动要新增独立链路，禁止在 `cost_budget.py` / `LLMBudgetCallbackHandler` 里混入 token 计数——预算 handler 的 `on_llm_end` 语义是「不再计数」（配额已在 start 预占，再记会把日限额腰斩），token 采集是另一关注点。
    - **埋点三位置 + 计价收敛**：LLM 走 `src/llm/usage_callback.py`（`LLMUsageCallbackHandler`，构造 ChatModel 挂 `[*budget_callbacks(), *usage_callbacks(backend, model)]`，两后端都要挂）；Tavily 在 `search()` 成功返回处；pkulaw 在 `PkulawMCPClient._run()` 成功返回前——**一个埋点覆盖 Agent 工具与固定管线两条路径**。失败/超限不记。金额计算全部收敛在 `src/observability/usage_store.py`（`llm_cost_cny` / `pkulaw_cost_cny` / `tavily_cost_cny`），埋点只传原始量。
    - **DeepSeek 缓存价差 50 倍**：缓存命中 ¥0.02 / 未命中 ¥1 / 输出 ¥2（每百万，2026-09-03 刊例）。ReAct 循环 system prompt+历史几乎次次命中，**不拆 cache hit/miss 会高估近一倍**。解析三级降级：usage_metadata.input_token_details.cache_read → response_metadata.usage.prompt_cache_hit_tokens → 全 miss 兜底。流式需 `ChatOpenAI(stream_usage=True)`；Ollama/拿不到 usage 时 tiktoken 估算标 `est=True`。
    - **存储**：`usage_logs` 表每次付费调用一行（source/model/tool/backend、cache_hit/miss_tokens、credits、est、`cost_cny` **金额快照**——写入时按当时价格表算好，改价不漂移历史，明细留原始 token/积分可重算）；`pricing` 表 key-value。**不建 rollup**（个人实例日几百行，GROUP BY 毫秒级；聚合统一走 `usage_store.read_usage_*`，将来量大换物化视图不动 API）。
    - **价格表**：`config.PRICING_DEFAULTS`（默认值）→ lifespan `ensure_pricing_defaults()` 首启幂等灌入 pricing 表 → 前端 `/usage` 页动态编辑（`PUT /api/usage/pricing`，只认 config 已知键，未知键忽略）。进程级价格缓存写后失效。
    - **写失败 debug 级吞掉、读失败 fail-open**——观测组件故障绝不拖垮主链路（与 query_logs / cost_budget 同原则）。
    - **API**（全部 `require_registered_user`，守护在 `tests/test_route_auth_guard.py`）：`GET /api/usage/summary?days=`（按日补零）/ `detail?day=&offset=&limit=` / `breakdown?days=&group=source|model|tool` / `pricing(GET|PUT)`。
    - **金额口径注意**：Tavily/北大法宝是**积分制**（Tavily 免费 1000 credits/月、法宝注册送 1 万+每日可领），面板金额按价格表折算**仅是估算参考**，真实支出以官方 console 为准——别把面板金额当成真实账单对外承诺。

12. **SSE 断线重连（D-M3-12 + D-0902-4，已实现）**：事件日志是重连补发的唯一真相源——`_bridge_sync_stream` 的 worker **先把事件写入 `StreamEventLog`（带 seq）再投递在线队列**，在线丢弃无妨。**只有主动取消（/chat/cancel）杀 worker**；被动断线（有日志）worker 继续跑完持续写日志，无 `request_id` 立即停。判定收敛在 `_on_exit_gone`，重放/跟进语义在 `resume_stream`。改桥接前必读：在线协程退出与 worker 生命周期是两条独立线，别把「杀 worker」挂在断开信号上（那正是本设计废除的旧行为）。
    - **归属校验（D-0902-4）**：`/chat/stream` 发起时用软鉴权身份登记流创建者（`StreamEventLog.set_owner`，TTL 同事件日志），`/chat/stream/resume` 校验「请求者 == 创建者」，不匹配 403——resume 重放的是问答全文，登录用户之间也要隔离。**新增能产生可重放流的接口时，必须同步登记 owner**，否则重放内容对所有登录用户裸奔。
    - **孤儿流宽限回收（D-0904-3）**：被动断线后 worker 继续跑，是为了让重连方补到完整内容；但用户刷新后不回来 / 直接关标签页时没有任何人会来认领，这一轮就纯白烧。故断线时给流打 deadline（`_ORPHAN_DEADLINES`，`STREAM_ORPHAN_GRACE_SECONDS` 默认 30s、配 0 关闭），worker 每次 yield 后检查、超时即 `gen.close()` 停止；`/chat/stream/resume` 到达**必须调 `_claim_stream()` 认领**（清除 deadline），重连连接自身断开时重新打 deadline。⚠️ 新增「能让流继续跑」的入口时，别忘了认领——漏认领的表现是用户重连后答案被拦腰截断。测试 `tests/test_stream_orphan_reclaim.py`。

13. **前端：离开会话时的生成语义（D-0904-1 / D-0904-2）**：答案落库由前端 `persistSession` 负责，所以"前端断开"必须成对处理——**要么真停（abort + `/chat/cancel`），要么让它跑完并落库，绝不能只 abort**（那是最差组合：后端照烧、结果谁也收不到）。当前语义：
   - **切换 / 新建会话 → 后台续跑并落库**：走 `stores/chat.js` 的 `switchSession()`（把仍有流在跑的旧会话现场保活到 `drafts[sid]`），SSE 继续写 `messagesOf(sid)`，跑完 `persistSession(sid, [msg])` 精确落回原会话。**不要再写 `abortController.abort()`**。
   - **刷新 / 切页回来 → 自动续流**：流快照持久化在 sessionStorage（`lawrag_pending_stream`，500ms 节流），`onMounted` 重建在途回答后调 `resumeChat(requestId, lastSeq)` 续流；快照超 9 分钟丢弃（事件日志 TTL 600s 留余量），续流失败降级提示「已中断，请重新提问」。快照存 sessionStorage 而非 localStorage：多标签页会话独立，不会把 A 标签页的流续到 B。
   - **只有「停止」按钮与登出才真取消**（abort + `/chat/cancel` + 清快照）；删除正在生成的会话同样立即停（答案已无处可落）。
   - **请求身份用 `myRequestId` 固化**：`consumeGeneration` 内一律用本请求 id 判断"我是否仍是当前请求"，不要读全局 `currentRequestId` 判断——切会话后新请求会覆盖它，旧流收尾会误清新请求状态 / 清不掉自己的 `activeStream`。

## 代码规范

- Python：类型注解（`from __future__ import annotations`）、模块级 docstring 说明"哪个需求/决策"、中文注释
- **复发错误先查 `docs/常见错误清单.md`**（绕过入口丢横切语义 / 漏 await / Fake 抹平差异 / 只看聚合指标等）；犯错的瞬间对照清单，修完回填新条目
- **新工具用 `@tool` 装饰器**（D-M3-3 语法 + D-M3-13 底层改为 LangChain），依赖经闭包注入，在 `tools/__init__.build_default_tools()` 注册：

  ```python
  def build_xxx_spec(client) -> ToolSpec:
      @tool(name="xxx", category=CATEGORY_WEB)
      def xxx(
          query: Annotated[str, "检索关键词"],
          # ⚠️ 枚举必须用 Literal，不要用历史上那个 Param 类（见下）
          kind: Annotated[Literal["a", "b"], "类型"] = "a",
          top_k: Annotated[int, "返回条数"] = 5,
      ) -> ToolResult:
          """工具描述（docstring 即 description，写给 LLM 看的路由依据）。"""
          ...
      return xxx
  ```

  规则：函数名即工具名（或显式 `name=`）；docstring 即 description；schema 由 **LangChain/pydantic** 从类型注解推导（无默认值即 required，会带上 `default`）；executor 异常全部内部消化返回 `ToolResult(ok=False)`。
  ⚠️ **枚举约束一律用 `Literal`**：本模块历史上的 `Param` 类 LangChain 不认识，会**静默丢弃**其中的 description 与 enum——不报错，只是发给模型的 schema 少了引导信息，极难发现。装饰器产出 `ToolSpec.langchain_tool`（`BaseTool`），`registry.langchain_tools()` 可直接喂给 `bind_tools()`。
- State 新字段：先在 `state.py` 的 `AgentState` TypedDict 声明，再在 `graph.py` 的 initial state 初始化
- 测试：外部服务一律 mock（见 `tests/fakes.py`），不许单测打真实网络

## 禁止事项

- ❌ 不在 Law-RAG-Agent 仓库写代码（只读上游）
- ❌ 不提交 `.env`、API Key、`面试问答-法律RAG智能问答系统.md`（用户私人文档，已 gitignore）
- ❌ 不接 Dify/Coze 等外部 Agent 平台（注：**D-M3-13 已推翻「不用 bind_tools」**，LLM 层现走 `BaseChatModel` + `bind_tools`；这里禁止的是外部低代码 Agent 平台，不是 LangChain 生态）
- ❌ 不重构前端框架、不更换检索底层存储（PRD 非目标）
- ❌ 工具/节点内不抛异常中断 ReAct 循环
- ❌ 测试不依赖真实网络与真实 API Key

## 完成标准（Definition of Done）

1. 单测覆盖新逻辑，`uv run pytest tests/ -q` 全绿（环境注意：conftest 已自动清空 `TAVILY_API_KEY` 与强制关闭连接池——db-mock 测试不再随本机 .env/常驻 PG 漂移，2026-09-03；本机 `CODEBUDDY_MCP_CONFIG` 会导致部分 @patch.dict 测试 teardown 报错，需 `env -u CODEBUDDY_MCP_CONFIG`）
2. 向后兼容：固定管线与旧 SSE 事件流不破坏
3. 更新 `CHANGELOG.md`；有架构/取舍决策则更新 `DECISIONS.md`
4. 涉及接口/行为变更时同步 `README.md` 与本文档
5. git 提交信息格式：`feat|fix|docs|refactor: 中文描述`

## 文档体系导航

| 文件 | 作用 |
| :--- | :--- |
| `AGENTS.md`（本文） | Agent 首要入口：全局信息、规范、DoD |
| `CHANGELOG.md` | 近期重要变更，防回归 |
| `DECISIONS.md` | 关键决策及原因，避免重复争论 |
| `docs/自主Agent重构PRD.md` | 重构总需求（EARS 原则、验收标准） |
| `docs/M1-架构设计.md` | M1 详细设计（D1~D7 决策、共享约定 §8） |
| `docs/M2联调结论-2026-08-28.md` | M2 验收结论：双路径口径、配额策略、前端渲染约定（前端/测试必读） |
| `docs/M4-多Agent路线图.md` | 多 Agent 演进唯一权威路线：两条核心原则、Agent 三问、目标拓扑、阶段与退出判据（**M4 开发必读**） |
| `docs/常见错误清单.md` | 复发错误知识库：症状→根因→预防，收录规则在内（**改代码前扫一眼**） |
| `docs/B2-法名推断spike报告-2026-08-30.md` | 法名向量最近邻选型报告：质心法 vs 描述文本法、Recall 数据、端到端接入设计（**B2 二阶段必读**） |
| `docs/M3-F12-人工确认技术方案.md` | F12 spike 结论：前置确认方案、checkpointer 选型、两种确认粒度成本对比、风险清单（**F12 开发必读**） |
| `docs/adr-*.md` | 历史检索配置 ADR |
