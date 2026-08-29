# CHANGELOG.md — 变更日志

> 记录有意义的变更，帮助 AI 快速了解最新动态、避免回归。格式参考 Keep a Changelog，新条目放最上面。

## [Unreleased] — M3 分场景确认（进行中）

- **refactor（重大）**：LLM 层与工具层迁移到 LangChain 标准生态（D-M3-13，推翻 D-M1-1「不用 bind_tools」）——目标为生态标准化、降低学习成本。
  - **阶段① LLM 层**：`OpenAICompatibleBackend` / `OllamaBackend` 内部改用 `ChatOpenAI` / `ChatOllama`，经 `.chat_model` 暴露（`.model` 仍是模型名字符串，18 处调用点不动）；`agent_node` 改为标准写法 `chat_model.bind_tools(schemas).invoke(messages)`
  - **阶段② 工具层**：`@tool` 装饰器底层改为委托 LangChain 的 `@tool`，**删除自研 schema 推导与 `Param` 类**（约 100 行）；`ToolSpec.langchain_tool` 持有 `BaseTool`，新增 `registry.langchain_tools()` 供 `bind_tools()` 直接消费
  - **预算熔断埋点改 callback**：新增 `src/llm/budget_callback.py`，`LLMBudgetCallbackHandler` 在 LangChain 调用链路上 check + record——上层可绕过 `LLMBackend` 三个公开入口直接 `invoke()`，挂 callback 才不会漏计。原 `base.py` 的 `_budget_check` / `_budget_record` 已删除
  - 重试策略**不变**：仍是自研 `is_retryable` + `wait_and_log`（D-M1-3），包在 LangChain 调用外层；`ChatOpenAI(max_retries=0)`
  - 依赖：`langchain-core` 1.4.8→1.6.1、`ollama` SDK 0.4.9→0.6.2，新增 `langchain-openai` 1.6.0、`langchain-ollama` 1.1.0
  - ⚠️ **迁移后确认的行为差异**（测试已同步）：① `str | None` → `anyOf [string, null]`（原扁平 string）② schema 带上 `default`（原不写，对 LLM 是有益引导）③ dataclass 类型展开为对象 schema（原降级 string）④ **枚举必须用 `Literal`**：历史上的 `Param` 类 LangChain 不认识，会**静默丢弃** description 与 enum（不报错，只是发给模型的 schema 少了引导信息，极难发现）
  - 顺带清理：删除从未被生产代码使用的 `OllamaLangChainWrapper`（ChatOllama 直接替代）
  - ⚠️ **遗留**：本机 Windows 安全策略拦截文件删除，`uv add` / `uv sync` / `uv lock` 在重建 jieba（只有 sdist、无 wheel）时静默失败，**`uv.lock` 尚未更新**（pyproject.toml 已正确声明）。需在能正常 lock 的环境补做

- **feat**：F11 场景分类配置（D-M3-10）——新增 `src/rag/scenes.py`，将用户查询映射到 PRD §5.2 的 10 个业务场景并判定 A 类（全自动）/ B 类（需人工确认）。
  - **场景清单是数据、分类逻辑是代码**：`SCENES` 元组即清单（id/名称/A\|B/关键词/工具），产品后续调整只改清单不动函数，「需产品定清单」因此不再阻塞开发
  - 打分三级权重：正则 3.0 > 强特征词 2.0 > 普通关键词 1.0。三级是必需的——中文查询里「合同」这类通用词会同时命中起草/审查/检索多个场景，必须让「起草」「审查」等强特征词和「第X条」正则压过通用词
  - 未命中任何场景时**保守回落**默认 A 类场景 `legal_qa`（`matched=False`），绝不因分类失败阻断回答（REQ-UW）
  - `AgentState` 新增 `scene_id` / `scene_kind` / `scene_matched` 三个字段，`ask()` 与 `stream()` 两条路径在意图识别后、进入图之前完成分类（REQ-E1）；**不新增图节点，不改动图结构**
  - 流式路径新增一条 `thinking` 事件展示场景识别结果（复用现有事件类型，前端无需改动）
  - 新增 `tests/test_scene_classification.py`（55 项），含关键词冲突回归用例（查法条不能被误判为合同类 B 类场景）与 5 项 graph 集成测试（stream 产出场景事件 / ask 写入 state / 闲聊跳过分类）
- **docs**：F12 人工确认节点技术底座 spike 结论——新增 `docs/M3-F12-人工确认技术方案.md` 与验证脚本 `scripts/f12_spike_demo.py`（5 项验证全过）。
  - **核心结论：F12 v1 不需要 `interrupt()`、不需要 checkpointer、不改图结构**。确认点放在进入图之前时图还没开始执行，没有任何状态需要保存恢复，一个「已确认」标记就够了。F12 由此从「M3 唯一会动执行架构的改动」降级为「不碰图的小改动」，成本约为逐步骤方案的 1/4（~2 人日 vs ~6-7 人日）
  - **重要实证（风险 R1）**：无 checkpointer 时 `interrupt()` **不报错**，图静默停住、答案为空，前端只收到挂起事件后永远等不到结果，后端日志无任何异常；`get_state` 才抛 `ValueError: No checkpointer set`、恢复时抛 `RuntimeError: Cannot use Command(resume=...) without checkpointer`。这个失效模式极隐蔽，若将来上逐步骤确认必须在图构建处加自检
  - 另：`ChatRequest.session_id` 已存在，可直接作为 `thread_id`（`f"{user_id}:{session_id}"`）；当前为单进程 uvicorn 部署，`MemorySaver` 可用但重启丢状态
- **docs**：F13 结论为**仅文档收尾**（D-M3-11）——保留 `sub_agent` state 字段，不建规划节点扩展位。无具体多 Agent 需求时的预留大概率推翻，触发重新立项的条件已写入 `DECISIONS.md`
- **docs**：SSE 断线重连继续生成方案定稿（D-M3-12，**代码待开发**）——**Redis 事件日志 + seq 游标重放**，不接 checkpointer：每个 SSE 事件带递增 seq 写入 Redis List（`stream:{request_id}:events`，TTL 10 分钟），重连时 `GET /api/chat/stream/resume?request_id=&after_seq=N` 先重放后订阅。
  - **区分主动停止与被动断线**（关键设计）：用户点 `/chat/cancel` → 立即停（省 Token，现状不变）；网络断开（`http.disconnect`）→ 继续跑完并持续写 Redis（LLM 调用已发生、成本已沉没，跑完即可重连补发）。与现有 `/chat/cancel` 机制天然契合，改造集中在 watchdog
  - **与 F12 人工确认正交，不要混为一谈**：一次确认是等人确认、发生在任何 LLM 调用之前；断线重连是网络断了接着看，属 SSE 层可靠性问题。checkpointer 保存的是**图状态**，解决不了「已流式输出的事件流怎么补给重连用户」——所以两个功能都不需要 checkpointer，PostgresSaver 不引入，零新增依赖
  - 工作量估算：后端 ~100-120 行 + 前端 ~80-100 行，约 2-3 人日
- **feat**：F14 预算熔断——新增 `src/observability/cost_budget.py`，监控外部付费 API 日用量并自动熔断。
  - 计数口径：LLM 按**逻辑调用次数**（埋点于 `LLMBackend` 公开入口，SDK 重试不重复计数）；Tavily 按次（按次计费，口径精确）。流式一次调用只计一次
  - 存储：Redis 原子 `INCR` + TTL 到次日零点自动失效；Redis 不可用时自动退化为进程内计数并告警，**不因监控组件故障拖垮主链路**
  - 熔断粒度：LLM 超限 → 整体熔断（API 前置检查，流式/非流式都返回友好提示）；Tavily 超限 → 只停网络搜索，工具返回 `ok=False`「搜索额度已用尽」，内部库与官方源照常，**回答正常生成**
  - 可观测：超限打 ERROR 日志（同日同种类仅一次）；新增 `GET /api/budget` 状态接口（需登录）
  - 配置：`BUDGET_ENABLED` / `BUDGET_MAX_LLM_CALLS_PER_DAY`(5000) / `BUDGET_MAX_TAVILY_CALLS_PER_DAY`(500) / `BUDGET_ENFORCE`(true，设 false 只告警不拦截)；阈值设 0 = 不限制
  - 新增 `tests/test_cost_budget.py`（23 项）
  - **实测参考**：一次复杂查询约消耗 18~20 次 LLM 调用（ReAct 多轮 + 校验），默认 5000 次 ≈ 250~280 次查询/天，部署时按实际用量调整

## 2026-08-28 — M2 双路融合（已完成）

> 联调结论见 `docs/M2联调结论-2026-08-28.md`（双路径口径 / 配额策略 / 前端渲染约定）。

- **feat**：前端引用溯源可视化（M2 / F10 收尾）——`ChatMessage.vue` 每条来源加验证状态徽章（内部库绿 / 官方源蓝 / 第三方橙 / 网络未验证灰），标题栏显示来源构成汇总，含未验证线索时顶部显示警示条；`App.vue` 主题新增 `--color-warning` / `--color-success-light`（light + dark 双套）
- **fix**：网络线索融合后被系统性挤掉——`fuse_evidence` 纯按 `fused_score` 排序截断，网络（≤0.5）永远排在权威来源（≥0.5）之后，`top_k=8` 被填满时一条都不展示，Tavily 调用成本花了用户却看不到。新增 `_truncate_with_web_quota` 保底配额（`FUSION_WEB_MIN_SLOTS`，默认 2，设 0 关闭）；新增 6 项测试
- **fix**：非流式 `/api/chat` 未接融合结果——此前直接用原始 `retrieved_docs`（实测 74 条、无 verification、无去重），与流式路径行为不一致。改为优先用 `fused_sources`（去重 + 来源加权 + 验证状态），实测 8 条；`_dicts_to_retrieved` 与 `ChatResponse.from_rag_answer` 同步支持 dict 形态并透传 `source` / `verification` / `url`；新增 `tests/test_chat_response_sources.py`（11 项）
- **refactor**：工具定义改用 `@tool` 装饰器（D-M3-3）——`base.py` 新增 `Param` 与 `@tool`，从类型注解 + `Annotated` 元数据推导 OpenAI JSON Schema、docstring 作 description，产出与手写 class 一致的 `ToolSpec`。三个内置工具改为 `build_xxx_spec(dep)` 工厂 + 闭包注入依赖；新增 `tests/test_tool_decorator.py`（13 项）。零第三方依赖，纯语法糖，工具行为与 schema 不变
- **refactor**：SSE 流式路径改为 LangGraph 图执行驱动（D-M3-1）——新增 `_build_react_loop_graph()`（纯 ReAct 循环子图，入口 agent），`_stream_react` 用 `graph.stream(state, stream_mode=["updates","values"])` 替换手写 `while` 循环；消息累积交由 `add_messages` reducer、循环终止交由条件边，SSE 事件从图事件流映射。删除 `_merge_stream_update`（手工补框架行为的补丁）与手写 guard 计数器
- **fix**：SSE 流式路径 ReAct 消息历史覆盖 Bug——`_stream_react` 用 `state.update()` 整体替换 messages，导致第 2 轮 `tool` 消息前丢失 `assistant(tool_calls)`，DeepSeek 返回 400 并降级 Ollama。当时用 `_merge_stream_update` 补丁对齐 `add_messages` 语义，新增 2 项回归测试（消息序列完整性 + user 查询保留）——**根因已由上条重构用框架消除，补丁随之删除，回归测试保留**
- **fix**：轮数上限强制作答时 DeepSeek 在纯文本输出 DSML 工具调用语法——`agent_node` 在 `schemas=[]` 时注入引导 system 消息（"禁止再输出工具调用语法"），并新增 `_strip_dsml_tool_calls` 兜底清除答案中的 DSML 块
- **fix**：flk.npc.gov.cn API 改版适配——端点 `/api/search` → `/law-search/search/list`（POST JSON），参数 `keyword` → `searchContent`、`searchType` 改为整型（2=模糊），响应 `result.data.records` → `rows`，状态码映射更新（3=现行有效、2=已修改、1=已废止、4=尚未生效），title 含 `<em>` 高亮标签需清除，详情 URL 由 `bbbs` 构造
- 新增 `src/search/legal_sources.py`：官方法律源客户端（国家法律法规数据库 API + 人民法院案例库域内搜索 + 小包公可选），封装为 `legal_source_search` 工具（PRD F9）
- 新增 `src/search/fusion.py`：双路结果融合去重、按来源加权排序、冲突裁决（内部库优先，网络结果标注验证状态）（PRD F6/F7/F8）
- `AgentState` 新增 `web_results` / `legal_results` / `fused_sources` 字段，`tools_node` 累计三路证据
- SSE `meta.sources` 携带 `source` 与 `verification` 状态（内部库 / 官方源已验证 / 网络未验证）（PRD F10）
- 更新 `REACT_SYSTEM_PROMPT`：三工具决策规则 + 引用必须标注来源与验证状态

## 2026-08-13 — M1 工具调用型 Agent（已完成）

- **fix（2939ab3）**：修复 DeepSeek V4 空 tool_call 空转 Bug——parallel_tool_calls 恒启用，模型想直接回答时返回 name="" 占位 tool_call，导致空转 4 轮到上限。`openai_backend`/`ollama_backend` 的 `_parse_tool_calls` + `react_nodes.agent_node` 三处过滤空 name；新增 `tests/test_empty_tool_call.py`
- **fix（2939ab3）**：前端 `ChatView.vue` runStream 增加 tool_call/tool_result 分支，thinkingTraces 升级 `{text,kind}` 结构，`ChatMessage.vue` 历史渲染兼容
- **feat（393170f）**：LexAgent 初始提交（173 files，+39120 行），完整项目骨架 + M1 代码；README 重写反映 M1 新能力
- M1 核心能力：`LLMBackend.chat_with_tools()` + LangGraph 手动 StateGraph ReAct（N=5）；`FailoverLLMBackend` 降级；`ToolRegistry` + `retrieve_knowledge` / `web_search`（Tavily）工具；SSE tool_call/tool_result 过程透传；454+ 测试通过，QA 独立复核 488 passed / 0 failed
- 项目迁移：M1 代码从 Law-RAG-Agent 迁入 LexAgent，上游仓库恢复干净（M1 新增代码已回退）

## 2026-08-12 — 重构 PRD 与任务拆解

- 产出 `docs/自主Agent重构PRD.md` v0.1：固定管线 RAG → 工具调用型自主 Agent，确认 9 个关键决策（双后端、Tavily、官方法律源、内部库优先、Docker Compose、预算熔断、SSE 透明化等）
- M1 任务拆解为 8 个子事项（配置就绪/后端切换/工具框架/ReAct/Tavily/SSE 透传/提示词/测试回归）

## 2026-08-03 — 检索质量与流式稳定性（继承自上游）

- 检索评测与优化：条件 BM25 混合（RRF，w=3.0），法条级 Hit@5 75.8%→86.1%；相邻扩展窗口 ±3→±1；章级摘要 chunk 过滤（`docs/检索评测与优化报告-2026-08-03.md`）
- 流式响应稳定性修复（`docs/流式响应稳定性修复报告-2026-08-03.md`）

## 2026-07-30 — 审计与测试基线（继承自上游）

- 代码审计与首轮测试报告；ADR-001 检索配置对齐、ADR-002 移除章级摘要
