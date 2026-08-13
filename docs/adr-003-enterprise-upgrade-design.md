# ADR-003: Law-RAG-Agent 企业级升级技术设计

> **状态**: 实施中
> **日期**: 2026-07-23
> **决策人**: 单人开发 + AI 辅助
> **前置阅读**: README.md、adr-001、adr-002
>
> ### 实施日志
> | 日期 | 步骤 | 内容 |
> |------|------|------|
> | 07-23 | 1a | LLM 后端抽象层 — base.py / ollama_backend.py / openai_backend.py / factory.py + 19 测试 ✅ |
> | 07-23 | 1b | Embedding 后端抽象层 — base.py / ollama_embedder.py / openai_embedder.py / factory.py + 24 测试 ✅ |
| 07-23 | 1c | config.py 统一配置 + adapter 适配器 + dependencies.py 接入 ✅ |
| 07-23 | 1d | 全面审查修复 — Bug1: LLMMessage兼容 + Bug2: 注释过期 + 6测试 ✅ |
| 07-23 | 1e | 二次审查 — Bug3: base_url污染(Ollama URL误入OpenAI后端) 已修复 ✅ |
| 07-24 | 2  | pgvector 完全替代 FAISS ✅ |
| 07-24 | 3  | 对话记忆层 — ConversationMemoryManager + memory_retrieve 节点 + 12 测试 ✅ |
| 07-24 | 3b | 软件工程重构 — graph.py 拆分为 state/prompts/nodes/graph 四文件 ✅ |
| 07-24 | 3d | 审查修复 — 移除未使用导入LLM_BASE_URL + 修复_create_embedder的base_url泄漏 ✅ |
| 07-24 | 4  | FAQ语义缓存 — FAQCache + Agent流式集成 + 5测试 (244 total) ✅ |
| 07-24 | 5  | 文档上传+解析管道 — PDF/DOCX/TXT解析器 + 清洗器 + 异步任务 + API端点 + 13测试 (257 total) ✅ |
| 07-24 | 5d | 审查修复 — Bug1: 状态查询永远404(pipeline单例) + Bug2: batch_size缺失 + 未使用io导入 ✅ |
| 07-24 | 6  | 意图识别增强+知识库扩展 — classify_query_type三分类 + doc_type路由检索 + 8测试 (265 total) ✅ |
| 07-25 | 7  | Token预算+幻觉防御+可观测性 — TokenBudget + HallucinationGuard + QueryLogger + 22测试 (287 total) ✅ |
| 07-25 | 8  | 前端新页面 — KnowledgeView(知识库上传) + HistoryView(对话历史) + 路由 + API ✅ |
| 07-25 | 8b | 步骤8审查 — 多维度前端+UI+交互检测: 文件校验 + thinkingOpen修复 + 进度条UI + 3项修复 ✅ |
| 07-25 | 7b | 步骤7审查 — 多维度安全+集成检测: ask()缺失防御 + 内容安全后置 + 关键词误杀 + MIN_SIM阈值硬编码 + 6项修复 ✅ |
| 07-24 | 6b | 步骤6审查 — 多维度质量检测: retriever签名兼容 + sanitize结果使用 + dead regex清理 + 269测试 ✅ |
| 07-25 | 6c | 回归修复 — _CASE_KEYWORDS 原子化拆分，恢复「有没有{任意}案子」等真实问句匹配能力 ✅ |
| 07-24 | 5e | 步骤5审查 — 多维度质量检测: 缺失导入修复 + asyncio阻塞修复 + 编码回退 + 8项修复 + 258测试 ✅ |
| 07-24 | 4b | 步骤4审查 — 多维度质量检测: sources反序列化 + ask()缓存一致性 + hit_count精确更新 + 245测试 ✅ |
| 07-24 | 3c | 步骤3审查 — 多维度测试：Prompt未格式化修复 + 代码去重 + 封装修复 + 215测试 ✅ |
| 07-23 | 1f | 三次审查 — 多维度安全审计 + 隐藏Bug检测 + 6项修复 ✅ |
| 08-03 | 9 | 记忆+上下文窗口升级 — save_memory幂等接线(会话保存异步固化) + importance预筛 + 指数时间衰减 + TokenBudget接入Agent/RAG动态窗口 + 12测试 (361 total) ✅ |
| 08-03 | 9b | Ollama num_ctx 下发 — OLLAMA_NUM_CTX 环境变量 + 自动取模型声明窗口 + 4测试 ✅ |
| 08-03 | 9c | 过期清理修复 — 清理循环失败重试(不再永久失效) + 对话记忆 clean_expired + 3测试 ✅ |
| 08-03 | 10 | FAQ缓存迁Redis Stack — FAQCacheRedis(向量检索+原生TTL+Set级联失效) + FAQ_CACHE_BACKEND开关 + docker-compose redis服务 + 16测试 (384 total) ✅ |

---

## 1. 升级目标

将当前 MVP 原型升级为可交付的私有化部署产品。

| 维度 | 当前 (v0.1) | 目标 (v0.5) |
|------|-------------|-------------|
| 记忆系统 | 截断最近 N 条历史消息 | 对话记忆 + FAQ 语义缓存 |
| 模型支持 | Ollama only | Ollama + OpenAI 兼容双后端 |
| 向量存储 | FAISS 为主 | pgvector 为主 |
| 知识库 | 30 部法律，静态 JSON | 多类型知识 + 文档上传 + 增量索引 |
| 意图识别 | 闲聊/法律 二分类 | 法律条文查询/案例参考/其他 三分类 |
| 可观测性 | logging 基础输出 | 结构化日志 + 检索质量追踪 |
| 部署 | docker-compose 单机 | docker-compose 单机（架构支持未来 K8s） |
| 缓存 | 无 | Redis (语义 FAQ + Embedding 缓存) |

---

## 2. 技术架构

### 2.1 整体架构

```
┌────────────────────────────────────────────────────────────┐
│                    Vue 3 前端 (Vite)                        │
│  页面：问答 | 知识库管理 | 对话历史                          │
└────────────────────────┬───────────────────────────────────┘
                         │ HTTP + SSE
┌────────────────────────▼───────────────────────────────────┐
│              FastAPI 应用服务 (单进程)                        │
│                                                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │ LLM 后端 │ │Embedding │ │  记忆系统 │ │ 知识库   │     │
│  │ 抽象层   │ │  抽象层   │ │  管理器   │ │ 管理器   │     │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘     │
│       │            │            │            │             │
│  ┌────▼────────────▼────────────▼────────────▼─────┐      │
│  │              LangGraph Agent (6→8 节点)           │      │
│  │  intent → memory_retrieve → rewrite → retrieve→ │      │
│  │            generate → validate → END             │      │
│  └───────────────────────┬─────────────────────────┘      │
└──────────────────────────┼────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼───────┐  ┌───────▼───────┐  ┌───────▼───────┐
│  PostgreSQL    │  │    Redis      │  │  Ollama/API   │
│  + pgvector    │  │  缓存+会话    │  │  LLM+Embed    │
└───────────────┘  └───────────────┘  └───────────────┘
```

### 2.2 新增模块清单

```
src/
├── llm/                        # [重构] LLM 多后端抽象
│   ├── base.py                 # 抽象基类 LLMBackend
│   ├── ollama_backend.py       # Ollama 实现
│   ├── openai_backend.py       # OpenAI 兼容 API 实现
│   └── factory.py              # 工厂函数 + Token 预算管理
├── embedding/                  # [重构] Embedding 多后端抽象
│   ├── base.py                 # 抽象基类 EmbeddingBackend
│   ├── ollama_embedder.py      # Ollama bge-m3
│   ├── openai_embedder.py      # OpenAI text-embedding-3
│   └── factory.py              # 工厂函数
├── memory/                     # [新增] 记忆系统
│   ├── __init__.py
│   ├── manager.py              # 记忆协调器
│   ├── conversation.py         # 对话记忆 (摘要 + 检索)
│   ├── faq_cache.py            # FAQ 语义缓存
│   ├── summarizer.py           # 对话摘要生成器
│   └── token_budget.py         # 上下文窗口 Token 预算管理
├── knowledge/                  # [新增] 知识库管理
│   ├── __init__.py
│   ├── models.py               # 统一文档 schema
│   ├── document_store.py       # PostgreSQL 文档存储
│   ├── ingestion/              # 文档解析管道
│   │   ├── pipeline.py         # 处理管道
│   │   ├── pdf_parser.py       # PDF 解析
│   │   ├── docx_parser.py      # Word 解析
│   │   └── text_cleaner.py     # 文本清洗
│   └── index_manager.py        # 增量索引管理
├── rag/                        # [扩展] RAG 引擎
│   ├── engine.py               # [修改] 集成记忆+Token预算
│   ├── retriever.py            # [重写] pgvector 主检索器
│   ├── hybrid_retriever.py     # [保留] 混合检索(备选)
│   ├── reranker.py             # [保留] Cross-Encoder 精排
│   ├── adjacent_expander.py    # [保留] 相邻条文扩展
│   └── intent.py               # [扩展] 三分类意图识别
├── agents/                     # [修改] Agent 工作流
│   ├── graph.py                # [修改] 增加 memory_retrieve 节点
│   └── tools.py                # [扩展] 新增知识库查询工具
├── api/                        # [扩展] 新增接口
│   ├── routes.py               # [修改] 新增知识库/记忆路由
│   ├── models.py               # [修改] 新增请求/响应模型
│   └── conversation_store.py   # [保留] 对话持久化
├── observability/              # [新增] 可观测性
│   ├── logger.py               # structlog 结构化日志
│   ├── trace.py                # 请求追踪
│   └── query_log.py            # 检索质量日志表
└── config.py                   # [修改] 新增配置项
```

---

## 3. 详细模块设计

### 3.1 多模型抽象层

#### 3.1.1 LLM 后端

```python
# src/llm/base.py - 抽象接口
from abc import ABC, abstractmethod
from typing import AsyncIterator

class LLMBackend(ABC):
    """LLM 后端抽象基类"""

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> str:
        """同步生成回答"""
        ...

    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式生成"""
        ...

    @abstractmethod
    def get_context_window(self) -> int:
        """返回该模型的上下文窗口大小 (tokens)"""
        ...
```

#### 3.1.2 配置驱动切换

```env
# .env
LLM_BACKEND=ollama           # ollama | openai | vllm
LLM_MODEL=qwen2.5:7b         # 模型名

# Ollama 配置
OLLAMA_BASE_URL=http://localhost:11434

# OpenAI 兼容配置 (DeepSeek / 通义千问 / vLLM / OpenAI)
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

#### 3.1.3 Embedding 后端

```python
# src/embedding/base.py
from abc import ABC, abstractmethod

class EmbeddingBackend(ABC):
    """Embedding 后端抽象基类"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化"""
        ...

    @abstractmethod
    def get_dimension(self) -> int:
        """返回向量维度"""
        ...

    @abstractmethod
    def get_model_name(self) -> str:
        """返回模型标识，用于 pgvector 维度隔离"""
        ...
```

---

### 3.2 记忆系统 MVP

#### 3.2.1 对话记忆

```
生命周期：跨会话，30 天

存储表 (PostgreSQL + pgvector):
┌─────────────────────────────────────────────────────┐
│ conversation_memories                                │
├─────────────────────────────────────────────────────┤
│ id              UUID PRIMARY KEY                    │
│ user_id         VARCHAR(128) NOT NULL               │
│ session_id      VARCHAR(128) NOT NULL               │
│ summary         TEXT              -- LLM 生成的摘要  │
│ summary_embed   VECTOR(1024)      -- 摘要向量        │
│ entities        JSONB             -- 关键实体         │
│   {case_type, laws_involved, parties, key_facts}    │
│ message_count   INT               -- 对话轮数         │
│ created_at      TIMESTAMPTZ                         │
│ expires_at      TIMESTAMPTZ       -- TTL: created+30d│
└─────────────────────────────────────────────────────┘

触发条件:
- 对话轮数 > 6 轮时触发异步摘要生成
- 摘要结构: {case_type, laws, key_entities, conclusion, open_questions}

检索流程:
1. 用户新问题 → embedding
2. pgvector 检索: WHERE user_id=xxx ORDER BY embedding <-> query LIMIT 3
3. 时间衰减: 越新的摘要权重越高
4. 拼入 Prompt [历史参考] 段
```

#### 3.2.2 FAQ 语义缓存

```
生命周期：TTL 7 天，关联法律变更时级联失效

存储表 (PostgreSQL + pgvector):
┌─────────────────────────────────────────────────────┐
│ faq_cache                                           │
├─────────────────────────────────────────────────────┤
│ id              UUID PRIMARY KEY                    │
│ question        TEXT NOT NULL                       │
│ question_embed  VECTOR(1024)                        │
│ answer          TEXT NOT NULL                       │
│ sources         JSONB             -- 引用来源         │
│ related_laws    TEXT[]            -- 关联法律ID列表   │
│ confidence      FLOAT             -- 置信度           │
│ hit_count       INT DEFAULT 1     -- 命中次数         │
│ created_at      TIMESTAMPTZ                         │
│ expires_at      TIMESTAMPTZ                         │
│ status          VARCHAR(20) DEFAULT 'active'        │
│   -- active | expired | invalidated                 │
└─────────────────────────────────────────────────────┘

命中条件:
- cosine_similarity > 0.95
- status = 'active'
- expires_at > NOW()

法律修订失效:
- 法律 V2 生效 → 级联标记 related_laws 包含该 ID 的所有缓存
- status: active → invalidated
```

#### 3.2.3 Token 预算管理

```python
# src/memory/token_budget.py

class TokenBudget:
    """上下文窗口 Token 预算管理器"""

    # 默认分配比例 (28K 窗口为例)
    ALLOCATION = {
        "system_prompt":    {"tokens": 800,   "priority": "required"},
        "memory_context":   {"tokens": 1500,  "priority": "high"},
        "retrieval_docs":   {"tokens": 8000,  "priority": "highest"},
        "chat_history":     {"tokens": 3000,  "priority": "medium"},
        "user_query":       {"tokens": 500,   "priority": "required"},
        "output_reserve":   {"tokens": 12000, "priority": "required"},
    }

    def __init__(self, context_window: int):
        self.total = context_window - 2000  # 预留 2K 安全边界

    def compute(self, query_complexity: str) -> dict:
        """根据查询复杂度动态调整分配"""
        ...

    def assemble(self, components: dict) -> str:
        """组装最终 Prompt，确保不超窗口"""
        ...
```

#### 3.2.4 LangGraph 工作流更新

```
当前 (6 节点):
intent → rewrite → retrieve → generate → validate

升级后 (8 节点):
intent → memory_retrieve → rewrite → retrieve → generate → validate
            ↑ 新增节点                              ↓
            └── 检索历史摘要                        ├─ PASS → END
                + FAQ 缓存检查                      └─ FAIL → generate (重试)
```

### 3.3 多集合并行检索

> **决策**: Phase 1 不做 LangGraph 多 Agent，在 retrieve 节点内部实现 asyncio.gather 并行检索。

#### 3.3.1 设计原则

```
多 Agent 与否的权衡：

┌──────────────────────────────────────────────────────────┐
│                      多 Agent 价值分析                     │
├──────────────┬──────────────┬──────────────┬─────────────┤
│  方案          │ 复杂度        │ 简单查询延迟  │ 复杂查询延迟  │
├──────────────┼──────────────┼──────────────┼─────────────┤
│ 单Agent串行    │ 低 (当前)     │ 快 2s         │ 慢 7s        │
│ 多Collection   │ 中 (推荐)     │ 一样快 2s      │ 快 4s        │
│ 并行检索        │              │              │             │
│ LangGraph      │ 高            │ 更慢 2.5s      │ 最快 3.5s    │
│ 多Agent        │              │              │             │
└──────────────┴──────────────┴──────────────┴─────────────┘

结论: 多集合并行检索 = 80% 多Agent价值 + 0% 多Agent复杂度
```

#### 3.3.2 架构

```python
# src/rag/retriever.py — 多集合并行检索

import asyncio

class MultiCollectionRetriever:
    """并行检索多个知识集合"""

    def __init__(self):
        self.collections = {}  # Phase 1 根据实际数据注册

    async def retrieve(self, query: str, intent: str, top_k: int):
        # 根据意图决定需要检索哪些集合
        collections = self._select_collections(intent)

        # 并行检索（asyncio.gather）
        tasks = [
            retriever.search(query, top_k)
            for name, retriever in collections.items()
        ]
        all_results = await asyncio.gather(*tasks)

        # 合并 + 统一精排
        return self._merge_and_rerank(all_results)

    def _select_collections(self, intent: str):
        """意图 → 检索集合映射"""
        if intent == "law_lookup":
            return {"law": self.collections["law"]}
        elif intent == "case_query":
            return {"case": self.collections["case"]}
        elif intent == "comprehensive":
            return self.collections  # 全部并行
        else:
            return {"law": self.collections["law"]}  # 默认
```

#### 3.3.3 多 Agent 预留

```python
# src/agents/graph.py — Phase 1 保持不变，架构预留

def build_graph():
    builder = StateGraph(AgentState)

    # retrieve 节点 — Phase 1: 单节点统一处理
    builder.add_node("retrieve", unified_retrieve_node)

    # Phase 2 替换方案 (不实现):
    # builder.add_node("retrieve_law", law_retrieve_node)
    # builder.add_node("retrieve_case", case_retrieve_node)
    # builder.add_node("retrieve_interpretation", interp_retrieve_node)
    # builder.add_node("merge_retrieval", merge_node)
    #
    # 用 LangGraph Send() 并行分发:
    #   retrieve → [Send("retrieve_law"), Send("retrieve_case")]
    #            → merge_retrieval
```

#### 3.3.4 触发条件（何时升级到多 Agent）

```
满足任意 2 项:
☐ 知识库扩展到 3 类以上 (法条+案例+司法解释+地方性法规)
☐ 复杂查询占比 > 30%
☐ 单次查询延迟 > 8s
☐ 不同知识类型需要完全不同的检索策略 (不同 embedding/reranker)
☐ 有真实反馈数据证明多 Agent 能解决实际问题

预计触发: Phase 2 中后期 (知识库 5万+ 向量文档时)
```

---

### 3.4 知识库管理系统

#### 3.4.1 统一文档 Schema

```json
{
  "doc_id": "law_0001",
  "doc_type": "law|interpretation|case|regulation",
  "title": "中华人民共和国刑法",
  "source": "全国人大",
  "effective_date": "1997-10-01",
  "version": 1,
  "status": "active|superseded|draft",
  "superseded_by": null,
  "chunks": [
    {
      "chunk_id": "law_0001_art_001",
      "chunk_type": "article|judgment|summary|guideline",
      "content": "为了惩罚犯罪，保护人民...",
      "embedding_model": "bge-m3",
      "metadata": {
        "chapter": "第一编 总则",
        "article_no": "第一条",
        "related_laws": [],
        "related_cases": []
      }
    }
  ]
}
```

#### 3.4.2 数据库表设计

```sql
-- 文档表
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_type VARCHAR(20) NOT NULL,  -- law|interpretation|case|regulation
    title VARCHAR(500) NOT NULL,
    source VARCHAR(500),
    effective_date DATE,
    version INT DEFAULT 1,
    status VARCHAR(20) DEFAULT 'active',  -- active|superseded|draft
    superseded_by UUID REFERENCES documents(id),
    original_filename VARCHAR(500),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 文档块表 (pgvector)
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    chunk_type VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    embedding_model VARCHAR(50) NOT NULL,
    embedding VECTOR(3072),             -- 按最大维度预留
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 检索质量日志
CREATE TABLE query_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL,
    user_id VARCHAR(128),
    query TEXT NOT NULL,
    intent VARCHAR(20),
    retrieved_count INT,
    reranked_count INT,
    faq_cache_hit BOOLEAN DEFAULT FALSE,
    memory_docs_used INT DEFAULT 0,
    llm_tokens_used INT,
    total_latency_ms INT,
    stage_timings JSONB,               -- {intent_ms, retrieve_ms, ...}
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

### 3.5 意图识别增强

```
当前: 闲聊 / 法律 (二分类)

升级后 (三分类):

用户输入 → intent 节点
         ├── casual        → 闲聊
         ├── law_lookup    → 法律条文检索 (主RAG)
         ├── case_query    → 案例检索 (语义匹配)
         └── other         → 超出知识库范围

每种意图对应不同检索策略:
- law_lookup: 精排 Top-5 法条 + 相邻条文扩展
- case_query: 语义检索 Top-3 案例 + 关联法条
- other: 不检索，提示超出范围

Prompt 模板也随意图动态切换
```

---

### 3.6 幻觉防御

```
四层校验:

Layer 1 (已有): 法条存在性检查 (validate 节点)
Layer 2 (已有): 引用精确性检查 (validate 节点)
Layer 3 (新增): 检索置信度检查
  - 检索结果 max_similarity < 0.7 → 视为低置信度
  - 回复"该问题超出当前知识库范围"，不强行生成
Layer 4 (强化): 免责声明
  - 自动在输出末尾追加
  - "以上内容基于现行法律法规整理，仅供参考，不构成法律意见"

内容安全（新增）:
- 输入过滤: 检测 "忽略指令"、"system prompt"、敏感词
- 输出过滤: 涉黄涉政关键词检测
- 攻击/违规 → 统一回复 "该问题不在我的服务范围内"
```

---

### 3.7 可观测性基础

```
每个请求记录：
{
  "request_id": "uuid",
  "user_id": "xxx",
  "query": "工伤认定标准...",
  "intent": "law_lookup",
  "retrieved_docs": 15,
  "reranked_docs": 5,
  "faq_cache_hit": false,
  "memory_docs_used": 2,
  "llm_tokens_used": 1240,
  "total_latency_ms": 3200,
  "stage_timings": {
    "intent_ms": 200,
    "memory_ms": 150,
    "rewrite_ms": 300,
    "retrieve_ms": 150,
    "rerank_ms": 300,
    "generate_ms": 2100
  }
}

存储: PostgreSQL query_logs 表
用途: 检索质量分析、性能瓶颈定位、高频问题发现

未来可接入: OpenTelemetry + Grafana
```

---

### 3.8 pgvector 性能防护

```
索引策略:
- document_chunks: IVFFlat (精度优先，list=sqrt(行数))
- faq_cache: HNSW (速度优先，m=16, ef_construction=200)
- conversation_memories: 精确搜索 (数据量小，无需近似索引)

维度优化:
- 使用 pgvector halfvec (半精度 float16)
  → 存储减半，检索速度提升 30-40%
  → 精度损失 < 0.1%，法律场景可接受

Embedding 模型隔离:
- embedding_model 字段标记每条向量的模型
- 查询时 WHERE embedding_model = current_model
- 切换模型 = 新建索引 + 后台并行重建 + 原子切换
```

---

## 4. 实施计划

### 4.1 Phase 1 任务清单（8 步，预计 4-6 周）

| # | 任务 | 产出 | 预计 |
|---|------|------|------|
| 1 | 项目结构重构 + 多模型抽象层 | 可切换 Ollama/OpenAI | 2-3 天 |
| 2 | pgvector 完全替代 FAISS | 增量索引 + 预留维度隔离 | 2-3 天 |
| 3 | 对话记忆层 | 跨会话摘要检索 | 2-3 天 |
| 4 | FAQ 语义缓存 | 高频问题直接响应 | 1-2 天 |
| 5 | 文档上传 + 解析管道 | PDF/Word → 向量入库 | 2-3 天 |
| 6 | 知识库扩展 + 意图识别增强 | 三分类 + 多类型知识 | 1-2 天 |
| 7 | Token 预算 + 记忆时序修复 + 幻觉防御 | 不爆窗口 + 时序正确 | 2-3 天 |
| 8 | 可观测性 + 前端新页面 | 日志 + KB管理 + 历史 | 2-3 天 |

### 4.2 编码顺序依赖

```
步骤 1 (多模型抽象层)
  └─→ 步骤 2 (pgvector) ──→ 步骤 5 (文档上传)
  └─→ 步骤 6 (意图识别)
  └─→ 步骤 3 (对话记忆) ──→ 步骤 4 (FAQ缓存)
       └─→ 步骤 7 (Token预算+时序修复) ──→ 步骤 8 (日志+前端)

步骤 1 是基础，必须先做。
步骤 2-4 可适度并行。
步骤 7 是集成步骤，需要 3+4+6 完成后再做。
```

---

## 5. 风险登记册

按致命程度排序：

| # | 风险 | 等级 | 防御措施 | 阶段 |
|---|------|------|----------|------|
| R1 | 检索失败后 LLM 自由发挥，给出虚构法律建议 | 🔴致命 | 设最低相似度阈值 0.7，低于阈值拒绝回答 | 步骤 7 |
| R2 | FAQ 缓存返回已废止法律的旧答案 | 🔴致命 | 缓存 TTL 绑定法律版本，修法时级联失效 | 步骤 4 |
| R3 | Prompt 注入 / 越狱攻击 | 🔴致命 | 输入输出关键词过滤，违规统一拒绝 | 步骤 7 |
| R3a | Prompt 注入 / 越狱攻击（MVP 防御） | 🔴→🟡 | ✅ 步骤 1f 已实施：`sanitize_input()` 11条注入模式 + 敏感词过滤 + 长度截断；完整输出过滤留待步骤 7 | 步骤 1f ✅ / 步骤 7 |
| R4 | Embedding 模型切换导致全量重建 | 🟡严重 | 表设计预留 embedding_model 字段隔离 | 步骤 2 |
| R5 | 流式输出 + 记忆检索时序冲突 | 🟡严重 | Graph 中 memory_retrieve 放在 retrieve 之前 | 步骤 7 |
| R6 | 法律修订时新旧条文并存 | 🟡严重 | 文档版本管理，status 字段标记，原子切换 | 步骤 5 |
| R7 | PDF 解析准确率不足 | 🟡严重 | 文本清洗管道 + 上传后预览确认 | 步骤 5 |
| R8 | pgvector 大数据量性能下降 | 🟢一般 | halfvec + 索引参数调优 + 预留 Qdrant 接入 | 步骤 2 |
| R9 | LangGraph Checkpoint 数据膨胀 | 🟢一般 | 定时清理过期 checkpoint | 步骤 7 |
| R10 | 对话数据隐私泄露 | 🟡严重 | 脱敏 + 数据库加密 + 删除接口 | 步骤 8 |

---

## 6. 部署与环境

### 6.1 开发环境

```
方案 B (开发用):
  LLM: DeepSeek / 通义千问 API
  Embedding: Ollama bge-m3 本地 GPU
  VRAM 需求: ~2.4GB (仅 Embedding)
  RAM 需求: ~12-14GB (PG + Redis + FastAPI + Ollama)

理由: RTX 4050 (6GB) 无法同时跑 LLM + Embedding
      API 方案效果远好于 7B 量化模型
```

### 6.2 客户部署配置

```
模式 1: 纯本地部署 (客户有 GPU)
  最低: RTX 3060 12GB + 32GB RAM
  推荐: RTX 4070 12GB + 64GB RAM
  组件: Ollama (LLM + Embedding) + PG + Redis + App

模式 2: API 混合部署 (客户用 API)
  最低: 16GB RAM (无 GPU 要求)
  组件: PG + Redis + App (+ Ollama 仅 Embedding)

模式 3: 纯 API 部署
  最低: 8GB RAM (无 GPU 要求)
  组件: PG + Redis + App
```

### 6.3 新增依赖

```toml
# pyproject.toml 新增
dependencies = [
    # 已有依赖保持不变 ...
    "openai>=1.0.0",           # OpenAI 兼容 API 客户端
    "redis>=5.0.0",            # 缓存
    "structlog>=24.0.0",       # 结构化日志
    "pymupdf>=1.24.0",         # PDF 解析 (比 pdfplumber 快 4x)
    "python-docx>=1.0.0",      # Word 解析
    "tiktoken>=0.7.0",         # Token 计数
]
```

---

## 7. 上下文窗口 Token 预算（完整规则）

### 7.1 静态分配 (28K 窗口基准)

```
┌─────────────────────────────────────────────────────┐
│  段                   │ 默认 Token │ 优先级   │ 可压缩  │
├─────────────────────────────────────────────────────┤
│  System Prompt        │    800     │ required │ 否     │
│  记忆上下文             │  1,500     │ high     │ 是     │
│  检索结果 (法条)        │  8,000     │ highest  │ 分两层  │
│  当前对话历史           │  3,000     │ medium   │ 是     │
│  用户问题              │    500     │ required │ 否     │
│  生成预留空间           │ 12,000     │ required │ 否     │
├─────────────────────────────────────────────────────┤
│  合计                  │ 25,800     │          │       │
│  弹性空间               │  2,200     │          │       │
└─────────────────────────────────────────────────────┘
```

### 7.2 动态调整 (根据查询复杂度)

```
简单查询 (法条查阅):
  检索 → 3K, 记忆 → 2K, 历史 → 4K, 生成预留 → 17K

一般查询 (案例咨询):
  检索 → 5K, 记忆 → 2K, 历史 → 3K, 生成预留 → 16K

复杂查询 (案情分析):
  检索 → 8K, 记忆 → 1K, 历史 → 1K, 生成预留 → 14K

对比分析 (法条对比):
  检索 → 10K, 记忆 → 500, 历史 → 500, 生成预留 → 13K
```

### 7.3 检索结果分层打包

```
层级 1 (必填): 精排 Top-5 法条原文 → 占检索预算 70%
层级 2 (有空间时): 相邻条文扩展 (±2) → 占检索预算 85% 上限
层级 3 (有空间时): 典型案例 Top-2 → 剩余预算

超出上限时从后往前裁剪
```

### 7.4 对话历史压缩

```
当前轮 → 保留原文
近 3 轮 → 保留原文，单条截断到 300 字符
3-6 轮 → LLM 压缩为 1-2 句摘要
6 轮以上 → 仅保留关键实体列表
```

---

## 8. API 设计

### 8.1 新增端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/knowledge/upload` | 上传文档 (multipart/form-data) |
| `GET` | `/api/knowledge/status/{task_id}` | 查询处理状态 |
| `GET` | `/api/knowledge/documents` | 文档列表 (分页) |
| `DELETE` | `/api/knowledge/{doc_id}` | 删除文档及向量 |
| `POST` | `/api/knowledge/reindex` | 重建全量索引 |
| `GET` | `/api/conversations` | 历史会话列表 |
| `GET` | `/api/conversations/{session_id}` | 会话详情 |
| `DELETE` | `/api/conversations/{session_id}` | 删除会话 |
| `POST` | `/api/feedback` | 提交回答反馈 (1-5 星 + 标签) |

### 8.2 Chat 请求扩展

```json
{
  "query": "工伤认定标准是什么",
  "session_id": "uuid",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "top_k": 5,
  "enable_memory": true,
  "enable_faq_cache": true
}
```

### 8.3 Chat 响应扩展

```json
{
  "query": "工伤认定标准是什么",
  "answer": "...",
  "sources": [...],
  "is_casual": false,
  "cache_hit": false,
  "confidence": 0.92,
  "memory_used": 1,
  "memory_summary": "用户之前咨询过工伤赔偿相关问题",
  "request_id": "uuid"
}
```

---

## 9. 验收标准

- [x] 多模型切换：`.env` 修改 `LLM_BACKEND` 即可切换，无需改代码
- [ ] 对话记忆：用户 A 新建对话后，系统能引用 30 分钟前对话 B 的关键信息
- [ ] FAQ 缓存：同一问题第二次查询时，命中缓存直接返回，延迟 < 100ms
- [ ] 文档上传：上传 PDF → 预览 → 确认入库 → 可检索，全流程可用
- [ ] 意图识别：法律查询 / 案例参考 / 超出范围 三类输出正确率 > 90%
- [ ] Token 预算：任何查询的最终 Prompt 不超过模型上下文限制
- [ ] 幻觉防御：检索置信度 < 0.7 时回复"超出知识库范围"而非编造
- [ ] 可观测性：query_logs 表完整记录每次查询的 5 个阶段耗时
- [ ] 不引入回归：174 个已有单元测试全通过
- [ ] 部署验证：`docker compose up -d` 一键启动全套服务

---

## 10. 附录：保留与删除

### 保留
- 所有现有测试用例 (174 个)
- 所有现有文档 (adr-001、adr-002、评测报告等)
- Docker Compose 部署方式
- LangGraph Agent 工作流框架
- JWT 认证体系
- 前端 Vue 3 + Pinia 体系

### 删除/替代
- `src/llm/client.py` → 重构为 `ollama_backend.py` + `openai_backend.py`
- `src/embedding/vector_store.py` (FAISS 管理) → 移至 `src/knowledge/index_manager.py` (pgvector 管理)
- `src/rag/retriever.py` (FAISS 检索器) → 重写为 pgvector 检索器
- `data/vector_store/` (FAISS 索引文件) → 数据迁移到 pgvector
- `scripts/build_index.py` → 替换为 `src/knowledge/ingestion/pipeline.py`

---

## 11. 步骤 1 审查记录 (2026-07-23)

| # | 发现 | 严重 | 修复 |
|---|------|------|------|
| 1 | `LLMAdapter` 不处理 `LLMMessage` 对象，`graph.py`/`engine.py` 传历史会 `AttributeError` | 🔴致命 | `_normalize_history()` 转换 + 6 测试 |
| 2 | `dependencies._create_embedder()` 注释写"回退到 LLM_BACKEND" | 🟢轻微 | 修正为"独立于 LLM_BACKEND" |
| 3 | `LawAgentGraph` 类型标注 `llm: LawLLM` 过时 | 🟡中等 | 运行时无影响，Phase2 重构修正 |

**审查通过项**：
- 无循环导入
- LLM/Embedding 后端独立选型正确
- `.env` 在 `.gitignore` 中
- 旧代码零破坏性变更

---

## 12. 步骤 3 审查记录 (2026-07-24)

| # | 发现 | 严重 | 修复 |
|---|------|------|------|
| 1 | `_create_embedder` 将 `EMBED_BASE_URL` 传给工厂函数，切换 OpenAI Embedding 时 URL 泄漏 | 🟡中等 | 移除 base_url 参数，由工厂从 env 读取 |
| 2 | `dependencies.py` 未使用导入 `LLM_BASE_URL` | 🟢轻微 | 已移除 |

---

## 13. 步骤 4 审查记录 (2026-07-24)

| # | 发现 | 严重 | 修复 |
|---|------|------|------|
| 1 | `stream()` FAQ缓存未命中时缩进错误，跳过整个 RAG 流程返回闲聊回答 | 🔴致命 | 移除错误缩进的 4 行 |
| 2 | 流程步骤注释编号重复 (2→2→3→4→5→6) | 🟢轻微 | 修正为 1→7 顺序编号 |

---

## 14. 步骤 5 审查记录 (2026-07-24)

| # | 发现 | 严重 | 修复 |
|---|------|------|------|
| 1 | `get_ingestion_status` 每次新建 Pipeline（_tasks={}），永远返回 404 | 🔴致命 | 改为模块级单例 `_get_ingestion_pipeline()` |
| 2 | `pipeline.run()` 调用 `self._embedder.batch_size`，`EmbeddingAdapter` 无此属性 | 🔴致命 | `EmbeddingAdapter.__init__` 添加 `self.batch_size` |
| 3 | `pdf_parser.py` `import io` 未使用 | 🟢轻微 | 已移除 |

---

## 12. 步骤 1f 审查记录 — 多维度安全审计 + 隐藏Bug检测 (2026-07-23)

### 12.1 审查维度

对步骤 1a~2 的全部 23 个变更文件进行了 5 维度扫描：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| SQL 注入 | 所有 SQL 拼接点，参数化查询使用 | 5 |
| Prompt 注入 | 用户输入是否进入 LLM prompt 不经过滤 | 3 |
| 认证安全 | 密码哈希、Token 管理、匿名用户回退 | 2 |
| 信息泄露 | API Key 日志输出、错误消息暴露 | 4 |
| 资源耗尽 | 连接池、重试机制、无上限输入 | 3 |

### 12.2 发现清单

#### 🔴 Critical — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **Prompt 注入无防御** — 用户输入直接进入 LLM prompt，可被 `忽略指令`/`输出system prompt`/`你不再是一个法律助手` 等越狱攻击利用 | `routes.py` `intent.py` | 内容安全风险 (ADR R3) | `intent.py` 新增 `sanitize_input()`: 11 条注入模式正则 + 敏感词过滤 + 长度截断；`routes.py` `/chat` 和 `/chat/stream` 入口集成过滤 |
| 2 | **迁移脚本 DELETE 无 COMMIT** — `scripts/migrate_faiss_to_pgvector.py:62-64` 执行 DELETE 后未 commit，清空操作不生效 | `migrate_faiss_to_pgvector.py` | 数据一致性问题 | 添加 `conn.commit()` + try/except/rollback + with 上下文管理 |
| 3 | **OllamaEmbedder.get_dimension() 无缓存** — 每次调用都发 API 请求，OpenAIEmbedder 有缓存但 Ollama 没有，`get_embedding_dim()`/`PgvectorRetriever._create_table()` 等高频调用路径浪费性能 | `ollama_embedder.py` | 性能浪费 | 添加 `_cached_dimension` + 已知模型维度表 (bge-m3=1024 等)，与 OpenAIEmbedder 对齐 |

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 4 | **config.py 导入时崩溃** — `float(os.getenv(...))` 若环境变量非法格式，整个模块导入失败，服务无法启动 | `config.py` | 启动失败 | 新增 `_safe_float()` / `_safe_int()` 辅助函数，格式错误时警告 + 使用默认值，替换全部 14 处 `float()/int()` 调用 |
| 5 | **AgentState TypedDict 缺键** — `validation_feedback` 字段在 stream/retry 路径中被使用但未在 TypedDict 中声明，类型检查器无法识别 | `graph.py` | 类型安全 | `AgentState` 添加 `validation_feedback: str` 字段 |
| 6 | **validate 节点不提取失败原因** — 校验 FAIL 时仅返回 `passed=False`，未从 LLM 输出中提取 `理由:` 内容，导致重试时 feedback 为空 | `graph.py` | 校验质量 | `_validate()` 解析 "理由" 后的文本作为 feedback，传递给 `_generate()` |

#### 🟡 High — 建议关注（不修不改行为）

| # | 发现 | 模块 | 风险 |
|---|------|------|------|
| 7 | **匿名用户静默回退** — `auth.py:get_current_user()` 在 Token 无效时静默回退到匿名用户而非返回 401，前端无法感知认证失败 | `auth.py` | 用户体验 |
| 8 | **Token 全量内存缓存** — `load_token_cache()` 将所有活跃 Token 加载到进程内存，进程被攻破则全部 Token 泄露 | `auth.py` | 纵深防御 |
| 9 | **psycopg2 无连接池** — `auth.py:_get_db()` 每次调用新建连接，高并发下连接数暴涨 | `auth.py` | 性能 |
| 10 | **全局单例无锁** — `get_llm()`/`get_engine()`/`get_agent()` 非线程安全，多线程首次并发调用可能创建多个实例 | `dependencies.py` | 并发安全 |

#### 🟢 Low — 已知不修

| # | 发现 | 模块 | 说明 |
|---|------|------|------|
| 11 | **类型标注过时** — `graph.py:18`/`engine.py:11` import `LawLLM` 作为类型但运行时使用 `LLMAdapter`，不影响功能 | `graph.py` `engine.py` | Phase 2 重构修正 |
| 12 | **流式重试可能重复输出** — `ollama_backend.py`/`openai_backend.py` 流式重试：若前次已 yield 部分 token 后失败，重试导致重复输出 | `ollama_backend.py` `openai_backend.py` | 概率极低，Phase 2 优化 |
| 13 | **无速率限制** — API 端点无 rate limiting，生产环境需 nginx/traefik 层配置 | `routes.py` | 部署层解决 |

### 12.3 SQL 注入审查结论

**全部 SQL 查询均使用参数化查询（`%s` 占位符）**，无用户输入直接拼接到 SQL 字符串的风险点：

| 文件 | 查询方式 | 安全性 |
|------|----------|--------|
| `pgvector_store.py:141-153` | `VALUES (%s, %s, ...)` 参数化 | ✅ 安全 |
| `pgvector_store.py:216-224` | WHERE 列名硬编码，值用 `%s` | ✅ 安全 |
| `retriever.py:257-268` | 全部 `%s` 占位符 | ✅ 安全 |
| `auth.py:85-88` | `VALUES (%s, %s, ...)` 参数化 | ✅ 安全 |
| `conversation_store.py:97-105` | `VALUES (%s, %s, ...)` 参数化 | ✅ 安全 |

唯一的 f-string 出现在 `PgvectorRetriever._create_table()` 的 `table_name` 和 `retriever.py:239` 的索引名，但这些值来自构造函数的默认参数 `"law_chunks"`，非用户可控。

### 12.4 新增安全测试建议

```
tests/test_security.py:
  - test_sanitize_injection_patterns     # 注入模式被拦截
  - test_sanitize_sensitive_keywords     # 敏感词被拦截
  - test_sanitize_normal_legal_query     # 正常法律查询不被误杀
  - test_sanitize_empty_input            # 空输入不崩溃
  - test_sanitize_long_input             # 超长输入被截断

tests/test_config_safety.py:
  - test_invalid_float_env               # 非法浮点数不崩溃
  - test_invalid_int_env                 # 非法整数不崩溃
```

### 12.5 步骤 1f 累计测试数

| 模块 | 原有 | 本次新增 | 合计 |
|------|------|---------|------|
| 全部 | 227 | 0 (仅修复，未新增测试) | 227 |

> 注：本次审查为代码级修复，安全测试用例建议在步骤 8（可观测性）阶段统一补充。

---

## 13. 步骤 3c 审查记录 — 多维度测试 (2026-07-24)

### 13.1 审查范围

对步骤 3 + 3b 的 9 个变更文件进行了 5 维度扫描：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| 安全 | SQL 注入、Prompt 注入、封装破坏 | 4 |
| 逻辑正确性 | Prompt 占位符、状态传递、反馈链路 | 3 |
| 代码质量 | 重复代码、类型安全、DRY 原则 | 4 |
| 架构 | 模块拆分合理性、单向依赖、路径一致性 | 5 |
| 边界情况 | 空记忆、无用户、PG 不可用、连接断开 | 3 |

### 13.2 发现清单

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **`_SUMMARY_PROMPT` 占位符未格式化** — `{conversation}` 以字面量传入 LLM，实际对话作为 user_message 而非替换到 system prompt 中，LLM 收到含 `{conversation}` 原始文本的系统消息 | `conversation.py:101` | 摘要质量下降 | `.format(conversation=conv_text)` 格式化后再发送 |
| 2 | **`_msg_role`/`_msg_content` 重复定义** — `nodes.py:23-37` 和 `graph.py:210-224` 各一份完整实现，存在漂移风险 | `nodes.py` `graph.py` | 维护隐患 | `graph.py` 改为 `from .nodes import` 导入，删重复代码 |
| 3 | **`stream()` 校验反馈硬编码** — 校验失败时写入固定字符串而非读取 validate 节点的实际返回，丢失审核器提供的具体错误原因 | `graph.py:201` | 重试质量下降 | 改为 `state.get("validation_feedback")` |
| 4 | **`ConversationMemoryManager` 封装破坏** — 直接访问 `PgvectorStore._conn` / `_ensure_connection()` 私有成员，依赖内部实现 | `conversation.py` `dependencies.py` | 耦合脆弱 | `ConversationMemoryManager` 改为独立持有 `psycopg2` 连接，`_create_memory_manager()` 不再创建 `PgvectorStore` |

#### 🟢 Medium — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 5 | **函数体内 import** — `nodes.py:190` 在 `generate_node` 内 `from src.llm.client import Message`，每次调用重复导入 | `nodes.py` | 微性能 | 提升到模块顶部 |

#### 🟢 Low — 已知不修

| # | 发现 | 模块 | 说明 |
|---|------|------|------|
| 6 | **`make_nodes()` 无类型标注** — `llm`/`retriever`/`memory_manager` 参数仅注释说明，无类型提示 | `nodes.py` | 当前均为 `Any`，Phase 2 统一补类型 |
| 7 | **`ask()`/`stream()` 路径分歧** — `ask()` 走 LangGraph graph（含 `memory_retrieve` 节点），`stream()` 手动步进（自己调 memory manager），行为性等价但结构不一致 | `graph.py` | 当前功能正确，属历史架构问题 |
| 8 | **无 `conversation_memories` 表存在性检查** — `ConversationMemoryManager` 假设表已由 `docker/init.sql` 创建，未做幂等建表 | `conversation.py` | 如果 init.sql 未执行会报 SQL 错误 |

### 13.3 架构评估

**步骤 3b 拆分质量** ✅：

| 评估项 | 评分 | 说明 |
|--------|------|------|
| 单向依赖 | ✅ 优秀 | `state` ← `prompts` ← `nodes` ← `graph`，无循环 |
| 关注点分离 | ✅ 优秀 | state 纯数据、prompts 纯模板、nodes 纯逻辑、graph 纯编排 |
| 闭包注入 | ✅ 优秀 | `make_nodes(llm, retriever, memory, top_k, max_retries)` 模式清晰，比全局变量/类属性更可控 |
| 零回归 | ✅ 通过 | step 3 12 测试 + 原有 203 测试 = 215 全通过 |

**记忆检索集成质量** ✅：

| 评估项 | 状态 | 说明 |
|--------|------|------|
| 内存检索降级 | ✅ | `memory_retrieve_node` 用 try/except 包裹，失败返回空 context |
| PG 不可用降级 | ✅ | `_create_memory_manager` 返回 None，Agent 在 `memory_manager=None` 模式正常工作 |
| 记忆注入位置 | ✅ | 记忆上下文放在法条前面（`nodes.py:179`），历史参考 → 法条原文，位置合理 |
| 时间衰减 | ✅ | `retrieve()` 中 7 天外的记忆线性衰减，过期记忆自然淘汰 |

### 13.4 SQL 注入审查：记忆层

| 文件 | 查询 | 安全性 |
|------|------|--------|
| `conversation.py:112-125` | INSERT `conversation_memories` WITH `%s` | ✅ 参数化 |
| `conversation.py:152-161` | SELECT `conversation_memories` WITH `%s` | ✅ 参数化 |
| `dependencies.py` | 仅传递 PG_CONN 字符串，不构造 SQL | ✅ 无 SQL |

### 13.5 步骤 3c 累计测试数

| 来源 | 测试数 |
|------|--------|
| step 1a-2 原有 | 203 |
| step 3 新增 (test_memory.py) | 12 |
| step 3b 零新增 | 0 |
| **合计** | **215** |

---

## 14. 新增模块 API 速查

### 14.1 ConversationMemoryManager

```python
from src.memory.conversation import ConversationMemoryManager

mgr = ConversationMemoryManager(
    conn_string="postgresql://...",
    embedder=embedder,
    llm=llm,
)

# 写入记忆（对话 ≥ 6 轮时自动触发摘要）
summary = mgr.save_memory(user_id, session_id, messages)

# 检索记忆
memories = mgr.retrieve(user_id, "工伤认定标准")
# → [{"summary": "...", "entities": {...}, "score": 0.85, ...}, ...]

context = mgr.build_context(memories)
# → "## 历史对话参考\n### 历史对话 1（相关度: 0.85）\n..."
```

### 14.2 Agent 模块拆分

```
src/agents/
├── state.py       # AgentState TypedDict
├── prompts.py     # REWRITE_PROMPT / VALIDATOR_PROMPT
├── nodes.py       # make_nodes() 工厂 + 9 个节点函数
└── graph.py       # LawAgentGraph 编排类
```

---

## 15. 步骤 4b 审查记录 — 多维度质量检测 (2026-07-24)

### 15.1 审查范围

对步骤 4（FAQ 语义缓存）的 5 个变更文件进行了 5 维度质量检测：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| 安全 | SQL 注入、缓存投毒、输入可信度 | 2 |
| 数据一致性 | 序列化/反序列化配对、类型安全 | 2 |
| 架构一致性 | `ask()`/`stream()` 路径行为对等 | 2 |
| 原子性 | hit_count 更新精度、并发场景 | 1 |
| 边界情况 | 重复存储、空 sources、连接断开 | 2 |

### 15.2 发现清单

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **`check()` 返回 sources 为 JSON 字符串** — `store()` 以 `json.dumps(...)` 写入，但 `check()` 未 `json.loads` 解析，前端收到的 `sources` 是字符串 `"[]"` 而非列表 `[]` | `faq_cache.py:100-117` | 前端展示异常 | `check()` 新增 `json.loads()` 解析步骤，返回 Python list |
| 2 | **`ask()` 路径缺少 FAQ 缓存** — `stream()` 入口检查 FAQ 缓存，`ask()` 直接进入 graph 无缓存检查，两条路径行为不对称 | `graph.py:98-113` | 内部调用无缓存加速 | `ask()` 开头新增 FAQ 缓存检查，与 `stream()` 行为对齐 |
| 3 | **hit_count 更新用 MIN 子查询** — `UPDATE ... WHERE distance = (SELECT MIN(distance))` 若两行距离相等则批量更新，虽概率极低但非原子 | `faq_cache.py:103-109` | 统计偏差 | 改为 `WHERE id = (SELECT id ... ORDER BY distance LIMIT 1)` 精确定位单行 |

### 15.3 SQL 注入审查：FAQ 缓存层

| 文件 | 查询 | 安全性 |
|------|------|--------|
| `faq_cache.py:84-92` | SELECT `faq_cache` WITH `%s` × 3 | ✅ 参数化 |
| `faq_cache.py:103-108` | UPDATE `faq_cache` WITH `%s` | ✅ 参数化 |
| `faq_cache.py:150-164` | INSERT `faq_cache` WITH `%s` × 7 | ✅ 参数化 |
| `faq_cache.py:181-184` | UPDATE `faq_cache` WITH `%s` | ✅ 参数化 |
| `faq_cache.py:195` | DELETE `faq_cache` (无外部输入) | ✅ 安全 |

### 15.4 缓存架构评估

| 评估项 | 状态 | 说明 |
|--------|------|------|
| 读路径降级 | ✅ | `check()` 异常返回 None，上层走 RAG 流程 |
| 写路径降级 | ✅ | `store()` 异常被 catch，不影响正常流程 |
| 级联失效 | ✅ | `invalidate_by_law(law_id)` 批量标记 `invalidated` |
| TTL 清理 | ✅ | `clean_expired()` + `INTERVAL '7 days'` 自动过期 |
| 阈值可配 | ✅ | `HIT_THRESHOLD = 0.95` 模块级常量 |
| 原子操作 | ✅ | `ORDER BY ... LIMIT 1` 精确更新单行 |

### 15.5 步骤 4b 累计测试数

| 来源 | 测试数 |
|------|--------|
| step 1a~4 原有 | 244 |
| step 4b 修复 | +1 (无新增测试) |
| **合计** | **245** |

---

## 16. 步骤 5e 审查记录 — 多维度质量检测 (2026-07-24)

### 16.1 审查范围

对步骤 5（文档上传+解析管道）的 11 个变更文件进行了 6 维度质量检测：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| 运行时正确性 | 导入完整性、asyncio 阻塞、方法存在性 | 3 |
| 安全 | 文件上传、路径遍历、编码攻击、无鉴权端点 | 4 |
| 数据完整性 | 分块逻辑、文本清洗、编码兼容 | 3 |
| 并发 | 连接安全、状态共享、单例 | 2 |
| 依赖完整性 | pyproject.toml、新依赖声明 | 1 |
| 边界情况 | 空文档、扫描件、加密 PDF、超长段落 | 4 |

### 16.2 发现清单

#### 🔴 Critical — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **缺失导入：`UploadFile`/`File`/`Form`/`Path`** — `routes.py:318` 使用但 line 10 import 不包含，模块加载即抛 `NameError` | `routes.py` | **端点启动崩溃** | 补全 `from fastapi import ... UploadFile, File, Form` + `from pathlib import Path` |
| 2 | **`logger` 未定义** — `routes.py:375` `_run_ingestion` 中 `logger.info()` 使用但路由模块无 `logger = logging.getLogger(...)` | `routes.py` | **后台任务崩溃** | 添加 `logger = logging.getLogger(__name__)` |
| 3 | **`asyncio.create_task` 阻塞事件循环** — `pipeline.run()` 是同步函数（PDF 解析/分块/向量化），直接在 async task 调用会阻塞整个 FastAPI 服务 | `routes.py:359` | **全服务阻塞** | 改为 `asyncio.to_thread(_run_ingestion_sync, ...)`，后台在线程池执行 |
| 4 | **缺少 `python-multipart` 依赖** — 文件上传端点需要 `python-multipart`，但 `pyproject.toml` 未声明 | `pyproject.toml` | **端点在无依赖环境下启动崩溃** | 添加 `python-multipart>=0.0.20` |

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 5 | **`.txt` 编码无回退** — `Path.read_text(encoding="utf-8")` 遇到 GBK/GB2312 编码的中文法律文档直接 `UnicodeDecodeError` | `pipeline.py:164` | GBK 文件解析失败 | UTF-8 失败时回退 `encoding="gbk"` |
| 6 | **`_split_paragraphs` 尾句生成孤 `。`** — `para.split("。")` 在段落以 `。` 结尾时产生空串，`s.strip() + "。"` → 单独的 `"。"` 被写入 chunk | `pipeline.py:193-196` | 噪声块 | 空串跳过 `if not s: continue` |
| 7 | **`has_text()` 缺少防御** — `import fitz` 无 try/except + `doc[0]` 在空文档时 `IndexError` | `pdf_parser.py:69-75` | 异常崩溃 | 添加 ImportError/IndexError/通用异常处理 |

#### 🟢 Medium — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 8 | **`_HEADER_FOOTER` `.*?全文` 误杀** — 正则 `.*?全文$` 匹配所有以"全文"结尾的行，包括 `《刑法》全文` 等合法内容 | `text_cleaner.py:29` | 误删合法文本 | 移除 `\|.*?全文` 分支，仅保留精确页眉模式 |

#### 🟢 Low — 已知不修

| # | 发现 | 模块 | 说明 |
|---|------|------|------|
| 9 | **上传端点无鉴权** — `/knowledge/upload` 未挂 `Depends(get_current_user)` | `routes.py:316` | MVP 阶段可接受，步骤 7 补 |
| 10 | **未验证文件内容类型** — 仅检查扩展名，攻击者可上传 `.exe` 重命名为 `.pdf` | `routes.py:332-335` | 解析器会优雅失败，无需额外防御 |
| 11 | **`parse()`/`parse_bytes()` 代码重复** — `docx_parser` 和 `pdf_parser` 中两版 `parse_bytes` 与 `parse` 有重复 | `docx_parser.py` `pdf_parser.py` | Phase 2 抽取公共抽象 |
| 12 | **`_ingestion_pipeline` 单例无线程安全** — 同 `dependencies.py` 中已知问题 | `routes.py:299` | 同步骤 1f 发现 #10 |

### 16.3 SQL 注入与路径遍历审查

| 文件 | 检查项 | 安全性 |
|------|--------|--------|
| `pipeline.py:105-110` | `ensure_document()` 参数均来自 task dict (内部可控) | ✅ 安全 |
| `pipeline.py:125` | `insert_chunks()` 参数化查询 | ✅ 安全 |
| `routes.py:316-366` | 文件上传使用 `tempfile.NamedTemporaryFile` + 扩展名白名单 | ✅ 安全 |

### 16.4 asyncio 阻塞修复说明

```
修复前:
  asyncio.create_task(_run_ingestion(...))      # async def → sync run() 阻塞事件循环

修复后:
  asyncio.create_task(asyncio.to_thread(         # 线程池执行，事件循环不阻塞
      _run_ingestion_sync, pipeline, task_id, tmp_path
  ))
```

### 16.5 步骤 5e 累计测试数

| 来源 | 测试数 |
|------|--------|
| step 1a~5 原有 | 257 |
| step 5e 修复（python-multipart 安装后 test_health 恢复） | +1 |
| **合计** | **258** |

---

## 17. 步骤 6b 审查记录 — 多维度质量检测 (2026-07-24)

### 17.1 审查范围

对步骤 6（意图识别三分类 + doc_type 路由检索）的 7 个变更文件进行了 5 维度质量检测：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| 运行时兼容性 | 旧检索器签名兼容、doc_type 参数传递链 | 3 |
| 安全 | sanitize_input 结果是否被实际使用 | 2 |
| 代码质量 | dead regex、重复 normalize、关键词整理 | 1 |
| 逻辑一致性 | ask/stream 三分类路径对齐、路由覆盖 | 2 |
| 回归 | 旧 classify_intent 兼容性、测试覆盖 | 3 |

### 17.2 发现清单

#### 🔴 Critical — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **旧检索器不支持 `doc_type` 参数** — `retrieve_node` 传递 `doc_type=doc_type`，`FAISSRetriever.search()` 和 `PgvectorRetriever.search()` 签名无此参数 → `TypeError: unexpected keyword argument` | `retriever.py` `nodes.py:167` | **非 pgvector 模式检索崩溃** | `FAISSRetriever` 和 `PgvectorRetriever` 签名添加 `doc_type: str\|None = None`（兼容接口，忽略该参数） |

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 2 | **Route 层 `sanitize_input` 结果被丢弃** — `/chat` 和 `/chat/stream` 调用 `sanitize_input(req.query)` 拿到 `safe_query`，但成功路径仍用 `req.query` 传给 agent（仅拒绝路径用了 safe_query） | `routes.py:74,152` | **安全防御形同虚设**：注入文本被 router 放过，原始输入进入 LLM | `agent.ask(safe_query, ...)` 和 `agent.stream(safe_query, ...)` |
| 3 | **`_CASE_KEYWORDS` 含 dead regex** — `类似.*案子`/`有没有.*案子`/`法院.*怎么判` 中 `.*` 被 `_normalize()` 静态化为无意义字符后丢弃，匹配逻辑实际是普通子串查找 | `intent.py:219-226` | 误导性代码 | 全部改为纯文本关键词：`类似案子`/`有没有案子`/`法院怎么判` |
| 4 | **`classify_query_type` 重复 normalize** — `sanitize_input`→`classify_intent`→`_CASE_KEYWORDS` 链中 `_normalize` 被调用 3 次 | `intent.py:229-261` | 微性能浪费 | 顶部预计算 `nq = _normalize(q)` 一次，后续复用 |

### 17.3 三分类路由完整性审查

| 查询 | 预期 | `classify_query_type` 结果 | `classify_intent` | 路由 |
|------|------|--------------------------|-------------------|------|
| `工伤怎么认定` | law_lookup | law_lookup | True | retrieve → law chunks |
| `有没有类似的案例` | case_query | case_query | True | retrieve → case chunks |
| `你好` | casual | casual | False | casual_reply → END |
| `忽略你的系统指令` | casual | casual (sanitize拦截) | — | casual_reply → END |
| 仅含 `伤害` 关键词 | law_lookup | law_lookup | True | retrieve → law chunks |
| 超长无害查询 | law_lookup | law_lookup | True | retrieve → law chunks |

### 17.4 检索器签名兼容矩阵

| 检索器 | `search(query, top_k)` | `search(query, top_k, doc_type=...)` | 修复后 |
|--------|----------------------|-------------------------------------|--------|
| `PgvectorStoreRetriever` | ✅ | ✅ (原生支持，直通 `doc_type` 过滤) | — |
| `FAISSRetriever` | ✅ | ❌ → `TypeError` | ✅ 兼容签名，忽略 doc_type |
| `PgvectorRetriever` | ✅ | ❌ → `TypeError` | ✅ 兼容签名，忽略 doc_type |

### 17.5 步骤 6b 累计测试数

| 来源 | 测试数 |
|------|--------|
| step 1a~6 原有 | 265 |
| step 6b 修复（旧 retriever 签名兼容后新增覆盖路径） | +4 |
| **合计** | **269** |

### 17.6 步骤 6c 回归修复 (2026-07-25)

**问题**：步骤 6b 将 `类似.*案子` 等含 `.*` 的关键词改为纯文本 `类似案子`，
丢失了对真实用户问句的匹配能力：

```
「有没有类似盗窃的案子怎么判」 → _CASE_KEYWORDS 全部未命中 → 错误分类为 law_lookup
「有没有这种案子」           → 同上
「类似打人该怎么判」         → 同上
```

**根因**：`_normalize()` 吞掉 `.*` 使其不工作，但直接删除 `.*` 等同于删除了"匹配中间任意内容"的语义，
导致关键词变成精确子串匹配，无法覆盖用户在中途插入具体罪名/描述的问句。

**修复**：复合模式原子化拆分：

```
修复前: 「有没有案子」 → 要求连续出现                 → 不匹配「有没有{盗窃}的案子」
修复后: [有没有][案子]  → 任一原子命中                 → 「有没有」命中 ✅
        [类似][怎么判]  → 「类似打人该怎么判」         → [类似][怎么判] 命中 ✅
```

| 查询 | 修复前 | 修复后 |
|------|--------|--------|
| `有没有类似盗窃的案子怎么判` | law_lookup ❌ | case_query ✅ |
| `有没有这种案子` | law_lookup ❌ | case_query ✅ |
| `类似打人该怎么判` | law_lookup ❌ | case_query ✅ |
| `工伤怎么认定` | law_lookup ✅ | law_lookup ✅ (无变化) |
| `你好` | casual ✅ | casual ✅ (无变化) |

---

## 18. 步骤 7b 审查记录 — 多维度安全 + 集成检测 (2026-07-25)

### 18.1 审查范围

对步骤 7（Token 预算 + 幻觉防御 + 可观测性）的 8 个变更文件进行了 6 维度检测：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| 安全 | 内容检查执行时序、ask 路径防御缺失、关键词误杀 | 3 |
| 集成完整性 | TokenBudget/QueryLogger 是否接入 agent、死依赖 | 4 |
| 配置 | 硬编码阈值、环境变量 | 1 |
| 路径一致性 | ask() vs stream() 防御对等 | 2 |
| 包完整性 | `__init__.py` 存在性 | 1 |
| 代码规范 | 函数体内 import、未使用 import | 2 |

### 18.2 发现清单

#### 🔴 Critical — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **`ask()` 路径无幻觉防御** — `stream()` 有 `HallucinationGuard.guard()`，`ask()` 无，非流式请求绕过多层防御 | `graph.py:98-113` | **防护缺口** | `ask()` 在 `_graph.invoke()` 后追加 `HallucinationGuard.guard()` |
| 2 | **内容安全检查后置** — `stream()` 先 `yield token` 流式输出全部回答，再调 `check_content_safety()`，不安全内容已送达用户 | `graph.py:225-241` | **不安全内容已外泄** | block 时 `yield {"type":"clear"}` 清除前端 + fallback |

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 3 | **`_OUTPUT_BLOCKED` 过度激进** — `"黑客"`/`"入侵"`/`"破解"` 等单字关键词直接阻断，"黑客入侵构成什么罪"是合法法律咨询 | `hallucination_guard.py:22` | **合法查询误杀** | 单字词改为完整教唆短语：`"教你如何黑客"`/`"如何入侵系统"` |
| 4 | **`MIN_SIMILARITY = 0.7` 硬编码** — 不同 embedding 模型阈值不同，bge-m3 需要 0.7，OpenAI text-embedding 可能需要 0.8 | `hallucination_guard.py:23` | 配置僵化 | 支持 `HALLUCINATION_MIN_SIM` 环境变量覆盖 |
| 5 | **`from ... HallucinationGuard` 在函数体内** — `stream()` 每个请求执行一次 import | `graph.py:236` | 微性能 | 提升到模块顶部 |

#### 🟢 Medium — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 6 | **`src/observability/` 缺少 `__init__.py`** | `observability/` | 非标准包 | 创建 `__init__.py` |

#### 🟢 Low — 已知不修 (阶段性)

| # | 发现 | 模块 | 说明 |
|---|------|------|------|
| 7 | **TokenBudget 未集成** — 完整的 Token 预算管理器已实现，但未接入 `nodes.py:generate` 的 prompt 构建 | `token_budget.py` | ✅ 已集成（步骤 9：接入 `nodes.py`/`graph.py`/`engine.py`，动态使用模型真实窗口） |
| 8 | **QueryLogger 未集成** — 链路追踪模块完整，但 `graph.py`/`routes.py` 均未使用 | `query_log.py` | 步骤 8 集成 |
| 9 | **`structlog>=26.1.0` 死依赖** — 已声明但无代码引用 | `pyproject.toml` | 步骤 8 预埋 |
| 10 | **`QueryLogger._save()` 每次新建连接** — `psycopg2.connect()` 每次写入创建/关闭，高并发下连接风暴 | `query_log.py:103` | 步骤 8 加连接池 |

### 18.3 安全时间线对比

**修复前** — 不安全内容的传输时序：

```
stream token "教你如何黑客入侵系统"  → 前端展示
... 更多 token ...
yield {"type": "thinking", "✅ 审核通过"}
check_content_safety("教你如何黑客入侵系统...")  ← 为时已晚
→ 拦截，yield fallback
```

**修复后**：

```
stream token "教你如何黑客入侵系统"  → 前端展示
... 更多 token ...
yield {"type": "thinking", "✅ 审核通过"}
check_content_safety("教你如何黑客入侵系统...")
→ 拦截 → yield {"type": "clear"} → yield fallback  ← 清除 + 替换
```

> 注：流式场景下内容安全为 best-effort 后置防御。生产环境建议在步骤 8 配合 TokenBudget 做前置过滤。

### 18.4 ask/stream 防御对等矩阵

| 防御层 | ask() | stream() |
|--------|-------|----------|
| 检索置信度 (MIN_SIM) | ✅ 修复后 | ✅ 已有 |
| 内容安全检查 | ✅ 修复后 | ✅ 已有 (后置) |
| Prompt 注入 (sanitize_input) | ✅ (路由层) | ✅ (路由层) |

### 18.5 步骤 7b 累计测试数

| 来源 | 测试数 |
|------|--------|
| step 1a~7 原有 | 287 |
| step 7b 修复 (+`__init__.py` 使 observability 包可用) | +1 |
| **合计** | **288** |

---

## 19. 步骤 8b 审查记录 — 多维度前端 + UI + 交互检测 (2026-07-25)

### 19.1 审查范围

对步骤 8（前端新页面）的 6 个变更文件进行了 4 维度检测：

| 维度 | 检查内容 | 检查文件数 |
|------|----------|-----------|
| 安全性 | 文件上传客户端校验、XSS 风险、鉴权集成 | 3 |
| 交互正确性 | SSE 事件处理、状态同步、清除/重试逻辑 | 2 |
| UI/UX | 布局合理性、信息层级、操作顺手度、配色一致性 | 3 |
| 异常处理 | 网络错误反馈、空状态展示、loading 状态 | 3 |

### 19.2 发现清单

#### 🟡 High — 已修复

| # | 发现 | 模块 | 影响 | 修复 |
|---|------|------|------|------|
| 1 | **文件上传无客户端校验** — 声称"最大 50MB"但 `onFileChange` 无大小检查，用户可选任意大小文件才发现后端拒绝 | `KnowledgeView.vue:88` | **用户体验差** | 添加 `MAX_SIZE = 50MB` 校验 + MIME type/扩展名双验证 |
| 2 | **`thinkingOpen` 状态丢失** — 切换对话/重新打开后思考过程默认折叠，用户无法看到已保存的推理链路 | `ChatView.vue:110` | **功能不完整** | `loadCurrentSession()` 恢复 `thinkingOpen.value = true` |
| 3 | **进度条文字被填充色覆盖** — `<span>` 在 `.progress-bar` 内部，`progress-fill` 伸展开时遮挡百分比文字 | `KnowledgeView.vue:57-60` | **UI 显示错误** | 文字移出 `.progress-bar`，独立为 `.progress-text`，右对齐 |

#### 🟢 Medium — 已知不修

| # | 发现 | 模块 | 说明 |
|---|------|------|------|
| 4 | **上传成功未重置表单** — 连续上传第二个文件需手动清空输入 | `KnowledgeView.vue` | Phase 2 优化 |
| 5 | **`catch { /* ignore */ }` 静默吞错** — 多处 API 调用失败无用户反馈 | `ChatView.vue:104,121,175` | Phase 2 加 toast |
| 6 | **删除确认用原生 `confirm()`** — 不可自定义、无 CSS 美化 | `HistoryView.vue:57` | Phase 2 替换为 dialog |
| 7 | **路由守卫无 token 过期处理** — 401 静默跳转 login，用户不知原因 | `router/index.js:19-26` | Phase 2 加提示 |

### 19.3 UI 设计评估

#### KnowledgeView（知识库管理）⭐ 良好

| 评估项 | 评分 | 说明 |
|--------|------|------|
| 信息层级 | ✅ | 上传卡片在上、任务卡片在下，符合操作流程 |
| 表单设计 | ✅ | 文档类型下拉 + 来源文本 + 日期选择器，字段合理 |
| 状态反馈 | ✅ | `pending→parsing→chunking→embedding→indexing→done` 六阶段 + 进度条 |
| 操作顺手 | ✅ | 选择文件→点击上传→轮询状态→去问答，一键直达 |

#### HistoryView（对话历史）⭐ 良好

| 评估项 | 评分 | 说明 |
|--------|------|------|
| 列表设计 | ✅ | 卡片式 session，ID + 消息数 + 时间，信息简洁 |
| 操作入口 | ✅ | `继续对话`(主操作) + `删除`(危险操作红色)，符合 Fitts 定律 |
| 空状态 | ✅ | `暂无历史对话` 居中提示 |

#### ChatView（问答主界面）⭐ 优秀

| 评估项 | 评分 | 说明 |
|--------|------|------|
| 布局 | ✅ | 侧栏 (260px) + 主区域，头部深紫 + 内容区浅紫，层次分明 |
| 思考过程 | ✅ | DeepSeek 风格折叠面板，箭头旋转动画 + spinner，专业感 |
| 导航 | ✅ | Header 中 `知识库` `历史` `退出` 右对齐，操作路径清晰 |
| 欢迎页 | ✅ | Logo + 标题 + 免责声明，首次使用引导 |

#### 整体配色 ⭐ 良好

| 变量 | 值 | 用途 |
|------|-----|------|
| `--color-primary` | `#7C3AED` | 主色 (按钮/进度条/链接) |
| `--color-primary-dark` | `#5B21B6` | Header 深紫背景 |
| `--color-primary-light` | `#EDE9FE` | 浅紫底色 (卡片/思考框) |
| `--color-bg` | `#FAF5FF` | 页面底色 |
| `--color-surface` | `#FFFFFF` | 卡片白底 |

紫色系一致、饱和度和明度层次分明，专业且不刺眼。

### 19.4 步骤 8b 累计测试数

| 来源 | 测试数 |
|------|--------|
| step 1a~8 原有 | 287 |
| step 8b 修复（仅前端） | +4 |
| **合计** | **291** |
