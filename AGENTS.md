# AGENTS.md — 项目说明书

> 所有 AI Agent 的首要入口。修改代码前必读；与实际不符时以代码为准并回头更新本文档。

## 项目概述

LexAgent 是一套**法律 RAG 智能问答系统**，正从固定管线 RAG 重构为**工具调用型自主 Agent**（详见 `docs/自主Agent重构PRD.md`）。

- 里程碑：M1 工具调用型 Agent（已完成）→ M2 双路融合（已完成，2026-08-28）→ M3 分场景人工确认 + 多 Agent 预留（进行中：F14 预算熔断已完成、F11 场景分类已完成、F12 已出技术方案待 Q5 决策、F13 结论为仅文档收尾）
- 双 LLM 后端：外接 API（DeepSeek，OpenAI 兼容）为主，Ollama 本地为降级
- 双路检索：内部 pgvector 知识库（最高优先级法律依据）+ 网络搜索（Tavily，仅作线索）+ 官方法律源二次验证
- 姊妹仓库 `Law-RAG-Agent` 为干净上游，**所有新代码只写在 LexAgent**

## 技术栈

| 层 | 技术 |
| :--- | :--- |
| 语言 | Python 3.12+（uv 管理依赖），前端 Vue3 + Vite |
| API | FastAPI + SSE 流式（`/api/chat`、`/api/chat/stream` 免认证；conversations/knowledge/crawl 需 Bearer） |
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
├── llm/             # LLM 后端：factory.py、openai_backend.py、ollama_backend.py、failover.py、retry.py
├── search/          # 外部搜索：tavily.py、legal_sources.py（M2）、fusion.py（M2）
├── rag/             # 检索链：retriever.py、engine.py、intent.py、scenes.py（M3/F11 场景分类）
├── api/             # FastAPI 路由
├── memory/          # 会话记忆 + hallucination_guard.py 幻觉守卫
├── observability/   # query_log.py 查询追踪
└── config.py        # 全部配置（.env 加载）
tests/               # pytest（FakeRetriever/FakeToolLLM，不依赖外部服务）
docs/                # PRD、架构设计、ADR、评测报告
```

## 常用命令

```bash
uv sync                                     # 安装依赖
uv run pytest tests/ -x -q                  # 全量测试（注意：需 TAVILY_API_KEY= 清空，见下）
uv run uvicorn src.api.main:app --reload    # 启动后端
docker compose up -d                        # pgvector / redis（本机已有旧容器 lawrag-db/lawrag-redis 时直接复用）
```

## 关键架构约定（改代码前必读）

1. **ReAct 循环走 LangGraph 图执行（D-M3-1）**：`agent_node` 调 `chat_with_tools` 决策 → `tools_node` 执行全部 tool_calls（并行）→ 回灌 → 循环；轮数上限 `AGENT_MAX_TOOL_TURNS=5`，达上限移除 tools 强制作答（REQ-UW4）。
   - **两条路径都走编译图**，禁止手写 `while` 循环步进节点：`ask()` 用 `_graph`（完整管线，入口 intent），`stream()` 用 `_react_loop_graph`（纯循环子图，入口 agent，D-M3-2）。
   - 消息累积由 `AgentState.messages` 的 `Annotated[list, add_messages]` reducer 保证，循环终止由条件边 `route_after_agent` 保证。**手工合并状态（如 `dict.update()` 覆盖 messages）会破坏 tool 消息与 `assistant(tool_calls)` 的配对关系，导致 DeepSeek 400 并降级 Ollama**（历史 Bug，勿重蹈）。
2. **回退路径**：`AGENT_REACT_ENABLED=false` 或主后端降级（Ollama）或 LLM 不支持工具 → 回退固定管线图（AC-7 向后兼容），**不许破坏**。
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
   - **场景清单是数据、分类逻辑是代码**：`src/rag/scenes.py` 的 `SCENES` 元组即清单（id/名称/A\|B/关键词/工具），`classify_scene()` 是逻辑。产品调整场景只改 `SCENES`，**不动任何函数**。打分用三级权重（正则 3.0 > 强特征词 2.0 > 普通关键词 1.0）——三级是必需的：中文查询里「合同」这类通用词会同时命中起草/审查/检索多个场景，必须让强特征词与「第X条」正则压过通用词，否则查个法条也会被判成 B 类要确认。
   - **分类在进图之前完成**：`ask()` / `stream()` 在意图识别后调用 `classify_scene()`，结果写入 `scene_id` / `scene_kind` / `scene_matched`。**不新增图节点、不改图结构**。
   - **F12 v1 确认点同样在进图之前**：B 类且未确认 → 产出 `confirmation_required` 事件并结束流；用户确认后前端重新发起请求。**v1 不接 `interrupt()`、不加 checkpointer、不改图**（理由见 `docs/M3-F12-人工确认技术方案.md`）。
   - ⚠️ 若将来要上「逐步骤确认」（v2，需在循环内中断），**必须先读该文档的风险 R1**：无 checkpointer 时 `interrupt()` **不报错**，图静默停住、答案为空，前端永远等不到结果而后端日志无任何异常。必须在图构建处加自检断言。
   - 未命中场景时**保守回落 A 类**（`matched=False`），绝不因分类失败阻断回答。
10. **LangChain 标准生态（D-M3-13）**：
   - LLM 层内部用 `BaseChatModel`（`ChatOpenAI` / `ChatOllama`），经 `.chat_model` 暴露。⚠️ `.model` **仍是模型名字符串**（历史字段，18 处调用点在读），两者别混淆。
   - 标准调用写法：`llm.chat_model.bind_tools(registry.langchain_tools()).invoke(messages)`；agent_node 已按此实现。
   - **新增 LLM 后端必须挂 `callbacks=budget_callbacks()`**：漏挂不会报错，只是预算不再计数，熔断形同虚设（测试 `test_callback_mounted_on_real_backends` 守着）。
   - **重试仍用自研的 `is_retryable` + `wait_and_log`**（D-M1-3），包在 LangChain 调用外层；**不要**改用 `ChatOpenAI(max_retries=)`——它的判定标准与 D-M1-3（4xx 不重试交由 Failover 降级、429/5xx 重试）不一致。
   - 消息转换统一走 `src/llm/base.py` 的 `to_langchain_messages()` / `tool_calls_from_langchain()`；后者已内置 D-M1-6 的空 name 过滤。
   - LangChain 的 tool_calls 参数是**已解析的 dict**（不像 OpenAI 原始响应是 JSON 字符串），因此不存在 `parse_error`，工具的容错改为「参数校验失败」路径。

9. **北大法宝 MCP 官方法律源（M3+ / F9 扩展，决策 D-PKULAW）**：接入 pkulaw.com 高权威源（法条原文 + 类案全文 + 核验 + 超链），优先级与现有官方源同级（`verified_official`）。
   - **懒加载 `mcp` SDK**：`src/search/pkulaw_mcp.py` 仅在真正调用时才 `import mcp`，未安装不影响模块导入与单测（单测一律用 `tests/fakes.FakePkulawClient`）。
   - **运行时按用途解析工具名**：pkulaw 聚合端点（默认 `mcp-law-agg`）把 10 个工具挂在一个 URL 下、名字带服务前缀且会变；客户端 `tools/list` 后按「用途关键词」匹配 name+description 建 purpose→name 映射，不硬编码工具名（SKILL 同款原则）。
     - ⚠️ **`_discover` 是协程，调用处必须 `await`**（历史 Bug）：漏 `await` **不报错**，只是 `_tool_map` 永远为空、静默退化为 `_FALLBACK_TOOL_NAMES`。而真端点实际工具名是**点分隔**（`mcp-law-search-service.search_article`），兜底快照是**下划线分隔**，一退化则**所有真实调用全部失败**且单测（Fake 绕过 `_a_call`）抓不到。回归测试见 `tests/test_pkulaw.py::TestPkulawToolDiscovery`。
     - 真端点已联调确认：10 个工具、8 个用途全部运行时命中零兜底；另有 `mcp-case.get_case_list`、`mcp-fatiao.get_law_item_content` 两个暂未映射用途，需要时再加进 `_PURPOSE_KEYWORDS`。
   - **参数平铺 + 结果按语义提取**：北大法宝工具 inputSchema 常声明包装体但后端只认平铺，一律传平铺；返回体形态不统一（裸数组/包裹体 `Data`/纯字符串），按字段语义而非名字取值，并清理链接锚点 `.0` 坏后缀。
   - **两条接入路径**：① 后端源——`PkulawLegalClient` 注册进 `LegalSourceClient` 门面，`legal_source_search` 自动融合（与既有国家库/案例库/小包公并列）；② ReAct 工具——`pkulaw_search`（检索）/ `pkulaw_verify`（核验+加链）按 `PKULAW_ENABLED` 与客户端可用性注册。
   - **预算熔断**：北大法宝按积分计费，新增 `KIND_PKULAW`（kind=`pkulaw`，`BUDGET_MAX_PKULAW_CALLS_PER_DAY` 默认 200），每次成功调用 `cost_budget` 先 check 后 record；超限工具层返回「法宝额度已用尽」、不阻断主链路（与 Tavily 同级降级语义）。
   - **配置只在 `.env`**：`PKULAW_MCP_URL` / `PKULAW_MCP_TOKEN`（聚合端点 Bearer），**严禁入库**（`.env` 已 gitignore）。

## 代码规范

- Python：类型注解（`from __future__ import annotations`）、模块级 docstring 说明"哪个需求/决策"、中文注释
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

1. 单测覆盖新逻辑，`uv run pytest tests/ -q` 全绿（环境注意：`TAVILY_API_KEY=` 清空；本机 `CODEBUDDY_MCP_CONFIG` 会导致部分 @patch.dict 测试 teardown 报错，需 `env -u CODEBUDDY_MCP_CONFIG`）
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
| `docs/M3-F12-人工确认技术方案.md` | F12 spike 结论：前置确认方案、checkpointer 选型、两种确认粒度成本对比、风险清单（**F12 开发必读**） |
| `docs/adr-*.md` | 历史检索配置 ADR |
