# LexAgent —— 法律 RAG 自主 Agent 智能问答系统

基于 LangGraph 的**自主 Agent** 法律智能问答系统。在 RAG 检索增强生成的基础上，升级为**工具调用型 Agent（ReAct 循环）**：Agent 自主决定调用「内部知识库检索」与「网络搜索」工具，支持实时法规/最新司法解释检索，并内置 LLM 多后端容灾降级与 SSE 执行过程透明化。

> 本项目从 `Law-RAG-Agent` 重构而来，M1（工具调用型 Agent）已完成并独立成仓。**不再依赖本地 GPU 部署 LLM**：默认调用云端 DeepSeek API，Ollama 本地模型仅作降级兜底。

---

## 核心特性

| 能力 | 说明 |
|:---|:---|
| 🤖 工具调用型 Agent | LangGraph 手动 StateGraph 实现 ReAct 循环（agent ⇄ tools，默认最多 5 轮），LLM 自主决策调用工具 |
| 🔍 内部知识库检索 | pgvector 向量检索 + BM25 混合 + bge-reranker 精排 + 相邻条文扩展 + 条款号精确路由 |
| 🌐 网络搜索 | Tavily 通用搜索工具，失败自动降级（返回「搜索不可用」不阻断），满足实时信息查询 |
| 🛡️ 双后端容灾 | DeepSeek API（默认）↔ Ollama（降级）；4xx 认证失败自动切换，429/5xx 走重试 |
| 🔄 向后兼容 | `AGENT_REACT_ENABLED=false` 一键回退原固定管线，旧 API 行为不变 |
| 📡 过程透明化 | SSE 透传 `tool_call` / `tool_result` 事件，前端可展示「正在调用 XX 工具」 |
| 🧠 记忆与防幻觉 | 会话记忆、FAQ 语义缓存、HallucinationGuard 幻觉守卫、Token 预算控制 |
| ⚖️ 数据合规 | 本地部署（物理机/VM + Docker Compose），数据主权可控，满足《数据安全法》 |

---

## 技术栈

| 层次 | 技术 |
|:---|:---|
| LLM | **DeepSeek API（默认，`deepseek-v4-flash`）** / Ollama Qwen2.5（降级） |
| 网络搜索 | Tavily API |
| Embedding | Ollama + bge-m3 (1024d) |
| Reranker | bge-reranker-v2-m3 (Cross-Encoder) |
| 向量索引 | pgvector (halfvec + HNSW) |
| Agent 框架 | LangGraph 1.2（手动 StateGraph） |
| 后端 | Python 3.12 / FastAPI 0.115 |
| 前端 | Vue 3 + Vite + Pinia |
| 认证 | JWT (python-jose) |
| 部署 | Docker + docker compose |

---

## 架构说明

### 两种运行模式

```
┌─ 模式一：ReAct 自主 Agent（默认，AGENT_REACT_ENABLED=true）─────────────────┐
│                                                                           │
│  用户提问 → intent 识别 → 场景分类 → memory 回忆                             │
│    → [ agent ⇄ tools 循环（≤5 轮）]                                        │
│        agent：LLM 自主决策（retrieve_knowledge? web_search? 直接回答?）     │
│        tools：执行工具并回灌结果                                             │
│    → validate 校验 → 生成最终答案（SSE 分块推送，含来源标注）                 │
│                                                                           │
│  工具集：retrieve_knowledge（内部库）· web_search（Tavily）                 │
│  特殊：DeepSeek V4 parallel_tool_calls 多工具并行执行；超轮数强制产出        │
└───────────────────────────────────────────────────────────────────────────┘

┌─ 模式二：固定管线（AGENT_REACT_ENABLED=false / 降级 Ollama）───────────────┐
│  intent → retrieve → generate → validate（失败重试）—— 原 RAG 链路，向后兼容 │
└───────────────────────────────────────────────────────────────────────────┘
```

### 容灾降级链路（FailoverLLMBackend）

```
DeepSeek API（主）
  ├─ 创建期缺 Key / 连接失败 → 直接降级 Ollama
  ├─ 运行期 4xx（认证/业务错误）→ 降级 Ollama 并重放请求
  └─ 429 / 5xx → 走现有 retry 重试机制（不降级）
降级后走固定管线（Q3 决策：小模型不做工具调用）
```

---

## 项目结构

```
LexAgent/
├── src/
│   ├── agents/                  # LangGraph Agent
│   │   ├── graph.py             # 图构建：ReAct 图（默认）/ 固定管线图（回退）
│   │   ├── react_nodes.py       # ReAct 节点：agent_node / tools_node / 路由
│   │   ├── state.py             # AgentState（含 tool_calls/tool_log/sub_agent 预留）
│   │   ├── prompts.py           # 系统提示词（含 REACT_SYSTEM_PROMPT）
│   │   ├── tools/               # 工具注册框架（M1 新增）
│   │   │   ├── base.py          # ToolSpec / ToolResult / truncate_summary
│   │   │   ├── registry.py      # ToolRegistry 注册表 + 异常归一化
│   │   │   ├── retrieve_knowledge.py  # 内部库检索工具
│   │   │   └── web_search.py    # Tavily 网络搜索工具
│   │   └── ...
│   ├── search/                  # 网络搜索（M1 新增）
│   │   └── tavily.py            # TavilySearchClient（超时/Key 校验/异常归一化）
│   ├── llm/                     # LLM 多后端
│   │   ├── base.py              # LLMBackend 抽象（含 chat_with_tools）
│   │   ├── openai_backend.py    # DeepSeek/OpenAI 兼容后端（工具调用）
│   │   ├── ollama_backend.py    # Ollama 本地后端（降级）
│   │   ├── failover.py          # FailoverLLMBackend 容灾降级（M1 新增）
│   │   ├── adapter.py / factory.py / retry.py
│   │   └── ...
│   ├── rag/                     # 检索核心（pgvector/BM25/rerank/混合/路由）
│   ├── knowledge/               # 知识处理（解析→切分→入库）+ 爬虫
│   ├── api/                     # FastAPI（认证/路由/SSE 透传/依赖注入）
│   ├── memory/                  # 会话记忆 + FAQ 缓存 + 幻觉守卫
│   └── config.py                # 全局配置（M1 新增 ReAct/Tavily/降级配置）
├── frontend/                    # Vue 3 前端（SSE 过程卡片待渲染 tool_call 事件）
├── scripts/                     # 业务/运维脚本
├── evaluation/                  # 评测（检索/回答质量/冒烟）
├── tests/                       # pytest（488 用例，含 M1 新增 test_tools/react_agent/failover/m1_qa_edge）
├── docs/                        # 文档
│   ├── 自主Agent重构PRD.md      # 重构需求文档（EARS，含 M1/M2/M3 里程碑）
│   ├── M1-架构设计.md           # M1 架构设计（PRD 评审/类图/时序图/任务）
│   ├── class-diagram.mermaid    # 类图
│   └── sequence-diagram.mermaid # ReAct 时序图
├── data/  LawData/  static/
├── pyproject.toml  uv.lock  docker-compose.yml  Dockerfile
└── .env.example
```

---

## 快速开始

> 前置：Python 3.12+、Node.js 18+、Docker。**无需本地 GPU**（默认调用 DeepSeek API）。

### 1. 配置环境变量

```bash
cp .env.example .env
# 必填（编辑 .env）：
#   OPENAI_API_KEY=<你的 DeepSeek API Key>
#   OPENAI_BASE_URL=https://api.deepseek.com/v1
#   OPENAI_MODEL=deepseek-v4-flash        # 注意：deepseek-chat 已于 2026-07 弃用！
#   AGENT_ENABLED=true                    # 开启 Agent
#   AGENT_REACT_ENABLED=true              # 开启 ReAct 工具调用循环（默认）
# 可选：
#   TAVILY_API_KEY=tvly-xxx               # 网络搜索（不配则搜索工具返回「搜索不可用」，系统仍可基于内部库回答）
#   LLM_FALLBACK_BACKEND=ollama           # 降级后端
```

### 2. 启动基础设施

```bash
uv sync                                  # 安装依赖（含 tavily-python）
docker compose up -d db redis            # PostgreSQL + pgvector + Redis
```

### 3. 导入法律数据（任选）

```bash
# a) 命令行爬虫直写 pgvector（国家法律法规数据库）
uv run python scripts/crawl.py --doc-type all --limit 50 --store pg
# b) LawData 批量导入
uv run python scripts/ingest_lawdata.py
```

### 4. 启动后端

```bash
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

### 5. 前端（可选）

```bash
cd frontend && npm install && npm run build   # 构建到 ../static，由 FastAPI 托管
# 开发模式：npm run dev（http://localhost:3000，/api 代理到 8000）
```

---

## API 接口

| 方法 | 路径 | 说明 | 认证 |
|:---|:---|:---|:---:|
| `GET` | `/api/health` | 健康检查 | — |
| `POST` | `/api/chat` | 法律问答（完整答案 + 引用来源） | Bearer |
| `POST` | `/api/chat/stream` | 流式问答（SSE，含 `tool_call`/`tool_result` 过程事件） | Bearer |
| `POST` | `/api/auth/register` / `/api/auth/login` | 注册 / 登录（JWT） | — |
| `POST` | `/api/crawl` | 触发爬取任务（增量） | — |
| `GET` | `/api/crawl/status/{task_id}` | 查询爬取状态 | — |

### SSE 新增事件（M1）

ReAct 模式下 `/api/chat/stream` 会在答案前推送 Agent 执行过程：

```json
{"event": "tool_call",   "data": {"tool": "retrieve_knowledge", "arguments": {"query": "民事诉讼法 最新修订"}}}
{"event": "tool_result", "data": {"tool": "retrieve_knowledge", "summary": "检索到 5 条相关条文…", "ok": true}}
```

前端据此渲染「正在调用 XX 工具」过程卡片，实现过程透明化。

---

## 环境变量（M1 新增/变更）

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `LLM_BACKEND` | `openai` | 主后端：`openai`（DeepSeek/OpenAI 兼容）或 `ollama` |
| `OPENAI_MODEL` | `deepseek-v4-flash` | **默认模型（deepseek-chat 已弃用，勿回退）** |
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1` | API 地址 |
| `OPENAI_API_KEY` | — | DeepSeek API Key（必填，否则降级 Ollama） |
| `LLM_FALLBACK_BACKEND` | `ollama` | 降级后端 |
| `LLM_FALLBACK_MODEL` | `qwen2.5:3b` | 降级模型 |
| `AGENT_ENABLED` | `true` | Agent 总开关 |
| `AGENT_REACT_ENABLED` | `true` | ReAct 工具调用循环开关（`false` 回退固定管线） |
| `AGENT_MAX_TOOL_TURNS` | `5` | ReAct 最大工具轮数（超限强制产出答案） |
| `TOOL_RESULT_SUMMARY_MAX_CHARS` | `300` | 工具结果摘要截断长度 |
| `TAVILY_API_KEY` | — | Tavily 搜索 Key（可选） |
| `TAVILY_MAX_RESULTS` | `5` | 搜索返回条数 |
| `TAVILY_TIMEOUT` | `15.0` | 搜索超时（秒） |

其余原 RAG 配置（`EMBED_*`、`RETRIEVAL_*`、`RERANK_*`、`ADJACENT_*`、`HYBRID_*`、`PG_CONN`、`JWT_SECRET` 等）保持不变，详见 `.env.example`。

---

## 核心功能

### RAG 检索流程（内部知识库）

```
用户查询
  → 条款号路由（法名+第X条精确置顶）
  → pgvector 向量检索（bge-m3, halfvec）
  → 相邻条文扩展（window=±1）
  → bge-reranker-v2-m3 精排（Top 15）
  → 条件 BM25 混合（加权 RRF）
  → 结果作为 retrieve_knowledge 工具产物回灌 Agent
```

检索质量（原系统评测，法条级 339 条测试集）：Hit@1 68.1% / Hit@5 86.1% / Hit@10 91.7%

### Agent 工作流（ReAct，M1）

```
intent → memory_retrieve
  → agent_node：LLM 决策
      ├─ 调 retrieve_knowledge（内部库）
      ├─ 调 web_search（Tavily 网络搜索，可并行）
      ├─ 已达 5 轮 → 移除工具，强制产出答案
      └─ 直接作答 → 进入 validate
  → tools_node：遍历执行全部 tool_calls（支持 parallel_tool_calls）
  → validate：答案校验（HallucinationGuard 豁免空文档场景）
  → END（SSE 分块推送最终答案，含来源标注）
```

### 切分策略

- **条文体**（law / regulation / constitution / supervision）：以「第X条」为边界独立成块，超长条文按句号拆分且续块保留条号前缀
- **非条文体**（judicial_interpretation / case）：按自然段切分，保持上下文连续
- 每个 chunk 携带层次元数据（法律名 → 条文范围），支撑引用溯源

---

## 评测结果

### M1 工具调用型 Agent（2026-08，QA 独立验证）

| 指标 | 数值 |
|:---|:---:|
| 测试用例 | **488 passed / 0 failed**（454 原有回归 + 34 M1 边界测试） |
| 全局一致性审查 | IS_PASS: YES |
| 源码缺陷 | 0（QA 独立复核，覆盖 ReAct 循环/工具异常归一化/降级/SSE/回退） |
| 已知遗留 | evaluation/scripts/smoke_test.py 会被 pytest 收集报 fixture 错误（建议 pytest ignore 该目录） |

新增测试：`tests/test_tools.py`、`tests/test_react_agent.py`、`tests/test_failover.py`、`tests/test_m1_qa_edge.py`

### 原系统检索/回答质量（重构前基线）

| 指标 | 数值 |
|:---|:---:|
| 检索 Recall@5 / @10 | 73.0% / 81.0% |
| 回答综合评分 | 0.890（真实幻觉率 0%） |

详见 `docs/retrieval_eval.md`、`docs/answer_quality.md`

---

## 测试

```bash
# 注意：本机若存在超大 CODEBUDDY_MCP_CONFIG 环境变量，需先清除避免 mock teardown 报错
env -u CODEBUDDY_MCP_CONFIG .venv/Scripts/python -m pytest -q
# 或 uv run pytest -q
```

---

## 里程碑路线

| 里程碑 | 状态 | 内容 |
|:---|:---|:---|
| **M1 工具调用型 Agent** | ✅ 已完成 | ReAct 循环、工具注册框架、Tavily 搜索、Failover 降级、SSE 透传、DeepSeek 默认后端 |
| M2 双路融合 + 法律垂直源 | 📋 规划中 | 内部库+网络双路检索融合裁决（内部库优先）、国家法律法规库/人民法院案例库、小包公案例补充 |
| M3 分场景确认 + 多 Agent | 📋 规划中 | A/B 场景分类（文书/合同类关键步骤人工确认）、多 Agent 任务规划（state 已预留 sub_agent） |

---

## 知识库与爬取

- 知识库基于 LawData 入库：**931 篇文档 / 51348 chunks**（doc_type：regulation 594 / law 295 / judicial_interpretation 40 / case 2）
- 内置「国家法律法规数据库」（全国人大官方，flk.npc.gov.cn）爬虫，增量落地 `LawData/`，支持 `pg`/`txt`/`both` 三种落库方式
- 数据仅用于学习/研究，请控制请求频率
- 案例（裁判文书）该数据源不提供，M2 规划引入第三方案例库补充

```bash
# 命令行爬虫
uv run python scripts/crawl.py --doc-type law --keyword 刑法 --limit 10 --store pg
```

---

## 免责声明

本系统回答基于现行法律法规整理，仅供参考，不构成专业法律意见。涉及具体法律事务，请咨询执业律师。
