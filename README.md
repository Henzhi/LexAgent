# LexAgent —— 法律 RAG 自主 Agent 智能问答系统

基于 LangGraph 的**自主 Agent** 法律智能问答系统。在 RAG 检索增强生成的基础上，升级为**工具调用型 Agent（ReAct 循环）**：Agent 自主决定调用「内部知识库检索」与「网络搜索」工具，支持实时法规/最新司法解释检索，并内置 LLM 多后端容灾降级与 SSE 执行过程透明化。

> 本项目从 `Law-RAG-Agent` 重构而来，M1（工具调用型 Agent）已完成并独立成仓。**不再依赖本地 GPU 部署 LLM**：默认调用云端 DeepSeek API，Ollama 本地模型仅作降级兜底。

---

## 核心特性

| 能力 | 说明 |
|:---|:---|
| 🤖 工具调用型 Agent | LangGraph 手动 StateGraph 实现 ReAct 循环（agent ⇄ tools，默认最多 5 轮），LLM 自主决策调用工具 |
| 🔍 内部知识库检索 | pgvector 向量检索 + BM25 混合 + bge-reranker 精排 + 相邻条文扩展 + 条款号精确路由 |
| 🌐 网络搜索 | Tavily 通用搜索工具 + 官方法律源（国家法律法规库/人民法院案例库/**北大法宝 MCP**），失败自动降级不阻断 |
| 🛡️ 双后端容灾 | DeepSeek API（默认）↔ Ollama（降级）；4xx / 重试耗尽自动切换，**冷却窗口后健康探测自动回切**，429/5xx 走重试 |
| 🔄 向后兼容 | `AGENT_REACT_ENABLED=false` 一键回退原固定管线；降级期间自动回落固定管线、恢复后回 ReAct（同一实例动态切换） |
| 📡 过程透明化 | SSE 透传 `tool_call` / `tool_result` 事件，前端展示「正在调用 XX 工具」；**断线自动重连续流**（事件日志 + seq 游标） |
| 🧠 记忆与防幻觉 | 会话记忆、FAQ 语义缓存、HallucinationGuard 幻觉守卫、Token 预算控制 |
| 🛑 预算熔断 | LLM / Tavily / 北大法宝按日计数（F14），LLM 超限整体拦截、外部源超限局部降级 |
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
| 认证 | Bearer Token（PBKDF2-SHA256 哈希存储，会话隔离） |
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
│  工具集：retrieve_knowledge（内部库）· web_search（Tavily）·                │
│          legal_source_search（官方源）· pkulaw_search/verify（北大法宝）     │
│  特殊：DeepSeek V4 parallel_tool_calls 多工具并行执行；超轮数强制产出；      │
│        B 类场景（起草/审查等）进图前一次人工确认（F12）                      │
└───────────────────────────────────────────────────────────────────────────┘

┌─ 模式二：固定管线（AGENT_REACT_ENABLED=false / 降级期间）───────────────────┐
│  intent → retrieve → generate → validate（失败重试）—— 原 RAG 链路，向后兼容 │
│  说明：主后端降级时自动回落本模式；failover 冷却探测回切后自动回到模式一      │
└───────────────────────────────────────────────────────────────────────────┘
```

### 容灾降级链路（FailoverLLMBackend）

```
DeepSeek API（主）
  ├─ 创建期缺 Key / 连接失败 → 直接降级 Ollama
  ├─ 运行期 4xx（认证/业务错误）→ 降级 Ollama 并重放请求
  ├─ 运行期 429 / 5xx → 走 retry 重试机制（不降级）
  └─ 重试耗尽（持续 429/5xx/网络）→ 抛 LLMRetryExhaustedError → 降级 Ollama

降级后进入冷却窗口（默认 300s）：
  ├─ 冷却期内全部请求直接走 Ollama（不再试探故障主后端）
  └─ 冷却结束后下一次真实请求兼作健康探测：成功 → 自动回切 DeepSeek；
     失败 → 继续降级并重置冷却窗口
降级期间走固定管线（Q3 决策：小模型不做工具调用）；回切后自动回到 ReAct 模式
```

---

## 项目结构

```
LexAgent/
├── src/
│   ├── agents/                  # LangGraph Agent
│   │   ├── graph.py             # 图构建：ReAct 图（默认）/ 固定管线图（降级/回退，运行时动态切换）
│   │   ├── react_nodes.py       # ReAct 节点：agent_node / tools_node / 路由
│   │   ├── state.py             # AgentState（含 tool_calls/tool_log/sub_agent 预留）
│   │   ├── prompts.py           # 系统提示词（含 REACT_SYSTEM_PROMPT）
│   │   ├── tools/               # 工具注册框架
│   │   │   ├── base.py          # ToolSpec / ToolResult / @tool 装饰器（schema 委托 LangChain）
│   │   │   ├── registry.py      # ToolRegistry 注册表 + 异常归一化 + 执行前 pydantic 校验
│   │   │   ├── retrieve_knowledge.py  # 内部库检索工具
│   │   │   ├── web_search.py    # Tavily 网络搜索工具
│   │   │   └── legal_source_search.py · pkulaw_search.py（含 search/verify 两工具）
│   │   └── ...
│   ├── search/                  # 外部搜索（M1/M2）
│   │   ├── tavily.py            # TavilySearchClient（超时/Key 校验/异常归一化）
│   │   ├── legal_sources.py     # 官方法律源门面（国家库/案例库/北大法宝）
│   │   ├── pkulaw_mcp.py        # 北大法宝 MCP 客户端（懒加载）
│   │   └── fusion.py            # 三路证据融合（internal > official > web）
│   ├── llm/                     # LLM 多后端（LangChain 生态，D-M3-13）
│   │   ├── base.py              # LLMBackend 抽象 + ToolCall/ToolCallResponse + Message
│   │   ├── openai_backend.py    # DeepSeek/OpenAI 兼容后端（工具调用）
│   │   ├── ollama_backend.py    # Ollama 本地后端（降级）
│   │   ├── failover.py          # FailoverLLMBackend 容灾降级 + 冷却自动回切
│   │   ├── retry.py             # 重试策略 + LLMRetryExhaustedError 哨兵
│   │   ├── budget_callback.py   # F14 预算熔断埋点（raise_error=True 请求内可中断）
│   │   └── factory.py / adapter.py
│   ├── rag/                     # 检索核心 + 场景分类（scenes.py F11/F12）
│   ├── knowledge/               # 知识处理（解析→切分→入库）
│   ├── embedding/               # bge-m3 Embedding 封装
│   ├── api/                     # FastAPI（认证/路由/SSE 透传/依赖注入/鉴权审计）
│   ├── memory/                  # 会话记忆 + FAQ 缓存 + 幻觉守卫 + confirmation_store
│   ├── observability/           # query_log（追踪）+ stream_log（SSE 重连日志）+ cost_budget
│   └── config.py                # 全局配置
├── frontend/                    # Vue 3 前端（SSE 过程卡片 / 断线续流 / 401 自动登出）
├── scripts/                     # 业务/运维脚本
├── evaluation/                  # 评测（检索 multi100/colloq148 / 回答质量）
├── tests/                       # pytest（810 用例：FakeRetriever/FakeToolLLM，不依赖外部服务）
├── docs/                        # 文档（详见 docs/README.md 索引）
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
#   POSTGRES_PASSWORD=<强随机口令，如 openssl rand -hex 24>
#       ↑ docker compose 必填变量（未设置会拒绝启动），生产务必改掉示例值
#   OPENAI_API_KEY=<你的 DeepSeek API Key>
#   OPENAI_BASE_URL=https://api.deepseek.com/v1
#   OPENAI_MODEL=deepseek-v4-flash        # 注意：deepseek-chat 已于 2026-07 弃用！
#   AGENT_ENABLED=true                    # 开启 Agent
#   AGENT_REACT_ENABLED=true              # 开启 ReAct 工具调用循环（默认）
# 可选：
#   TAVILY_API_KEY=tvly-xxx               # 网络搜索（不配则搜索工具返回「搜索不可用」，系统仍可基于内部库回答）
#   PKULAW_MCP_URL / PKULAW_MCP_TOKEN     # 北大法宝 MCP（法条/类案权威源，默认不启用）
#   LLM_FALLBACK_BACKEND=ollama           # 降级后端
```

### 2. 启动基础设施

```bash
uv sync                                  # 安装依赖（含 tavily-python）
docker compose up -d db redis            # PostgreSQL + pgvector + Redis
# 注：db 容器不再映射 5432 到宿主（仅 compose 网络内被 app 访问）；
#     需要宿主机直连调试时另行 docker run 一个带映射的实例
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

认证口径（2026-09-01 审查整改后）：`—` 匿名可用；`软` 匿名可用但登录则绑定身份（会话/配额归属）；`🔒` 需登录（`require_registered_user`，无有效 token 返回 401）。

| 方法 | 路径 | 说明 | 认证 |
|:---|:---|:---|:---:|
| `GET` | `/api/health` | 健康检查 | — |
| `POST` | `/api/chat` | 法律问答（完整答案 + 引用来源） | 软 |
| `POST` | `/api/chat/stream` | 流式问答（SSE：tool_call/tool_result/meta/token/confirmation_required） | 软 |
| `POST` | `/api/chat/cancel` | 主动取消生成（立即停，省 Token） | 软 |
| `POST` | `/api/chat/confirm` | F12 人工确认（B 类场景 approved/取消） | 软 |
| `GET` | `/api/chat/stream/resume` | 断线重连：按 seq 游标重放 + 跟进新事件（校验流归属） | 🔒 |
| `POST` | `/api/rewrite` | 智能改写 / 案情分析（预算前置 + 20 次/分钟 IP 限流） | 🔒 |
| `GET` | `/api/budget` | F14 预算用量与阈值（运维） | 🔒 |
| `POST` | `/api/auth/register` · `POST` `/api/auth/login` | 注册 / 登录（Bearer token） | — |
| `GET` | `/api/auth/me` | 当前用户 | 软 |
| `GET/POST/DELETE` | `/api/conversations/{session_id}` | 会话历史读取/保存/删除（用户隔离） | 软 |
| `POST` | `/api/knowledge/upload` · `GET` `/api/knowledge/status/{task_id}` | 文档上传（异步解析入库）/ 状态 | 🔒 |
| `GET` | `/api/knowledge/documents` · `GET` `.../{doc_id}/chunks` · `DELETE` `.../{doc_id}` | 文档分页 / 正文分块 / 删除（防匿名拉库） | 🔒 |
| `GET` | `/api/crawl/types` | 爬虫类型列表 | 🔒 |
| `POST` | `/api/crawl` · `GET` `/api/crawl/status/{task_id}` | 触发国家法律法规库增量爬取 / 状态 | 🔒 |

### SSE 新增事件（M1 / M3）

ReAct 模式下 `/api/chat/stream` 会在答案前推送 Agent 执行过程：

```json
{"event": "tool_call",   "data": {"tool": "retrieve_knowledge", "arguments": {"query": "民事诉讼法 最新修订"}}}
{"event": "tool_result", "data": {"tool": "retrieve_knowledge", "summary": "检索到 5 条相关条文…", "ok": true}}
{"event": "confirmation_required", "data": {"scene": "...", "scene_name": "合同起草", "prompt": "...", "options": [...], "confirm_id": "..."}}
```

前端据此渲染「正在调用 XX 工具」过程卡片与 F12 确认卡；事件带递增 `seq` 字段供断线续流游标使用。

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
| `PKULAW_MCP_URL` / `PKULAW_MCP_TOKEN` | — | 北大法宝 MCP 聚合端点与 Bearer token（仅在 .env，严禁入库） |
| `BUDGET_*` | 阈值 0 = 不限制 | F14 预算熔断（`BUDGET_ENFORCE=false` 只告警不拦截）；LLM 一次复杂查询约 18~20 次调用 |
| `STREAM_LOG_TTL_SECONDS` | `600` | SSE 重连事件日志 TTL（与归属登记同量级） |
| `CONFIRMATION_TTL_SECONDS` | `600` | F12 人工确认标记有效期（Q7 决策） |

> 注：failover 降级回切冷却窗口（默认 300s）是 `FailoverLLMBackend` 构造参数（`recovery_cooldown_seconds`），未暴露环境变量；设 0 禁用自动回切保持旧语义。

其余原 RAG 配置（`EMBED_*`、`RETRIEVAL_*`、`RERANK_*`、`ADJACENT_*`、`HYBRID_*`、`PG_CONN`、`JWT_SECRET`、`POSTGRES_PASSWORD`（compose 必填）等）保持不变，详见 `.env.example`。

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

### 当前状态（2026-09-02）

| 指标 | 数值 |
|:---|:---:|
| 自动化测试 | **810 passed / 0 failed**（47 个测试文件，全部离线 mock，不依赖外部服务） |
| 代码审查整改 | 2026-09-01/02 两轮整改完成（审查报告见 `docs/代码审查报告-2026-09-01.md`） |
| 检索评测基准 | multi100（100 语义查询）/ colloq148（148 口语化查询），运行手册 `docs/检索评测运行手册.md` |

### M1 工具调用型 Agent（2026-08，QA 独立验证）

| 指标 | 数值 |
|:---|:---:|
| 测试用例（当时） | **488 passed / 0 failed**（454 原有回归 + 34 M1 边界测试） |
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
| **M1 工具调用型 Agent** | ✅ 已完成（2026-08） | ReAct 循环、工具注册框架、Tavily 搜索、Failover 降级、SSE 透传、DeepSeek 默认后端 |
| **M2 双路融合 + 法律垂直源** | ✅ 已完成（2026-08-28） | 三路证据融合（内部库优先）、国家法律法规库/人民法院案例库/北大法宝、断线重连 |
| **M3 分场景确认 + 多 Agent** | ✅ 已完成（2026-08-30） | F14 预算熔断、F11 场景分类、F12 人工确认（进图前一次确认）、LangChain 生态迁移 |
| **M4 多 Agent 演进** | 📋 已立项待启动（D-M4-1） | 路线见 `docs/M4-多Agent路线图.md`；M4 代码未启动 |

---

## 知识库与爬取

- 知识库以 LawData 文本 + `article_map.json`（991 部法律 / 46520 条法条索引）入库 pgvector（chunk 规模以数据库实际为准）
- 内置「国家法律法规数据库」（全国人大官方，flk.npc.gov.cn）增量爬虫，落地 `LawData/`，支持 `pg`/`txt`/`both` 三种落库方式（`uv run python scripts/crawl.py`）
- 案例（裁判文书）：官方无公开 API，走人民法院案例库域限定搜索发现线索（M2，D-M2-3）；北大法宝 MCP 提供法条/类案权威检索与核验（F9 扩展）
- 数据仅用于学习/研究，请控制请求频率

```bash
# 命令行爬虫
uv run python scripts/crawl.py --doc-type law --keyword 刑法 --limit 10 --store pg
```

---

## 免责声明

本系统回答基于现行法律法规整理，仅供参考，不构成专业法律意见。涉及具体法律事务，请咨询执业律师。
