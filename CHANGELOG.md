# CHANGELOG.md — 变更日志

> 记录有意义的变更，帮助 AI 快速了解最新动态、避免回归。格式参考 Keep a Changelog，新条目放最上面。

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
