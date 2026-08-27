# AGENTS.md — 项目说明书

> 所有 AI Agent 的首要入口。修改代码前必读；与实际不符时以代码为准并回头更新本文档。

## 项目概述

LexAgent 是一套**法律 RAG 智能问答系统**，正从固定管线 RAG 重构为**工具调用型自主 Agent**（详见 `docs/自主Agent重构PRD.md`）。

- 里程碑：M1 工具调用型 Agent（已完成）→ M2 双路融合（进行中）→ M3 分场景人工确认 + 多 Agent 预留
- 双 LLM 后端：外接 API（DeepSeek，OpenAI 兼容）为主，Ollama 本地为降级
- 双路检索：内部 pgvector 知识库（最高优先级法律依据）+ 网络搜索（Tavily，仅作线索）+ 官方法律源二次验证
- 姊妹仓库 `Law-RAG-Agent` 为干净上游，**所有新代码只写在 LexAgent**

## 技术栈

| 层 | 技术 |
| :--- | :--- |
| 语言 | Python 3.12+（uv 管理依赖），前端 Vue3 + Vite |
| API | FastAPI + SSE 流式（`/api/chat`、`/api/chat/stream` 免认证；conversations/knowledge/crawl 需 Bearer） |
| 编排 | LangGraph 1.2 StateGraph（手动 ReAct，**不用** langchain bind_tools） |
| LLM | 自研 `LLMBackend.chat_with_tools()`；DeepSeek `deepseek-v4-flash`（主）+ Ollama qwen2.5（降级） |
| 检索 | pgvector(halfvec+HNSW) + BM25 条件混合（RRF）+ bge-reranker 精排 + 相邻扩展 |
| 存储 | PostgreSQL/pgvector + Redis（FAQ 语义缓存） |
| 搜索 | Tavily（通用）；官方法律源（国家法律法规数据库 flk.npc.gov.cn、人民法院案例库 anli.court.gov.cn） |

## 目录结构

```
src/
├── agents/          # LangGraph 编排：graph.py（图）、react_nodes.py（ReAct）、nodes.py（固定管线）、state.py、prompts.py
│   └── tools/       # 工具层：base.py（ToolSpec/ToolResult）、registry.py、retrieve_knowledge.py、web_search.py
├── llm/             # LLM 后端：factory.py、openai_backend.py、ollama_backend.py、failover.py、retry.py
├── search/          # 外部搜索：tavily.py、legal_sources.py（M2）、fusion.py（M2）
├── rag/             # 检索链：retriever.py、engine.py、intent.py
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

1. **ReAct 循环**：`agent_node` 调 `chat_with_tools` 决策 → `tools_node` 执行全部 tool_calls（并行）→ 回灌 → 循环；轮数上限 `AGENT_MAX_TOOL_TURNS=5`，达上限移除 tools 强制作答（REQ-UW4）。
2. **回退路径**：`AGENT_REACT_ENABLED=false` 或主后端降级（Ollama）或 LLM 不支持工具 → 回退固定管线图（AC-7 向后兼容），**不许破坏**。
3. **工具失败不抛异常**：统一返回 `ToolResult(ok=False)`，summary 首词为错误标签（如"搜索不可用"），ReAct 循环继续。
4. **来源优先级**：内部库 `internal_kb` > 官方源 `legal_source` > 网络 `web`。网络结果仅作线索，作为法律依据必须回源官方库二次验证。
5. **summary 截断**：工具结果 summary ≤300 字符（`TOOL_RESULT_SUMMARY_MAX_CHARS`），防上下文膨胀。
6. **空 tool_call 过滤**：DeepSeek V4 想直接回答时会返回 name="" 的占位 tool_call，`agent_node` 必须过滤（历史 Bug 2939ab3）。
7. **模型名**：deepseek-chat 已于 2026-07-24 弃用，用 `deepseek-v4-flash`。

## 代码规范

- Python：类型注解（`from __future__ import annotations`）、模块级 docstring 说明"哪个需求/决策"、中文注释
- 新工具：实现 `XxxTool.build_spec() -> ToolSpec`，在 `tools/__init__.build_default_tools()` 注册，executor 异常全部内部消化
- State 新字段：先在 `state.py` 的 `AgentState` TypedDict 声明，再在 `graph.py` 的 initial state 初始化
- 测试：外部服务一律 mock（见 `tests/fakes.py`），不许单测打真实网络

## 禁止事项

- ❌ 不在 Law-RAG-Agent 仓库写代码（只读上游）
- ❌ 不提交 `.env`、API Key、`面试问答-法律RAG智能问答系统.md`（用户私人文档，已 gitignore）
- ❌ 不用 langchain bind_tools / 不接 Dify/Coze 等外部 Agent 平台
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
| `docs/adr-*.md` | 历史检索配置 ADR |
