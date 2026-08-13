# 单元测试报告

**生成时间**: 2026-07-20
**测试框架**: pytest 9.1.1
**CI 兼容**: 是（零外部依赖，无需 Ollama / PostgreSQL）

---

## 一、测试概览

| 指标 | 数值 |
|:---|:---:|
| 测试文件数 | 5 |
| 用例总数 | **174** |
| 通过 | **174** |
| 失败 | 0 |
| 执行耗时 | 8.86s |

---

## 二、各文件详情

### 2.1 test_chunking.py（44 用例）

| 测试类/函数 | 数量 | 说明 |
|:---|:---:|:---|
| `test_cn_to_int_valid` | 25 | 中文数字→整数：一→1、一百二十三→123、一千→1000 等 |
| `test_cn_to_int_zero` | 1 | "零" → 0 |
| `test_cn_to_int_empty` | 1 | 空字符串 → 0 |
| `test_law_document_basic / with_hierarchy` | 2 | LawDocument 数据模型构造与嵌套层次 |
| `test_article_model` | 1 | Article dataclass 字段验证 |
| `test_section_model` | 1 | Section 与 Articles 关联 |
| `test_chapter_with_articles` | 1 | Chapter 直接挂条文（无节） |
| `test_build_article_context` | 2 | 层次上下文元数据构建（完整/空层级） |
| `test_build_context_prefix` | 2 | 上下文前缀生成（完整/最小） |
| `test_article_range_single/multi/two` | 3 | 条文范围描述：单条/多条 |
| `test_chunk_config_defaults/custom` | 2 | ChunkConfig 默认值与自定义 |
| `test_retrieved_doc_citation_*` | 3 | citation 属性：完整/无条文/空 |
| `test_to_retrieved_*` | 3 | LangChain Document → RetrievedDoc 转换 |

---

### 2.2 test_rag_engine.py（33 用例）

| 测试类/函数 | 数量 | 说明 |
|:---|:---:|:---|
| `test_is_casual_query_true` | 31 | 闲聊正则命中：你好/谢谢/再见/自我介绍等 |
| `test_is_casual_query_false` | 16 | 法律问题不误判：法条号/罪名/合同等 |
| `test_is_casual_query_introducing_self` | 1 | "介绍一下你自己" 命中闲聊 |
| `test_format_sources_*` | 5 | 来源格式化：空/闲聊/单条/去重/多法律 |
| `test_build_prompt_*` | 5 | RAG Prompt 构建：有文档/空/多组/去重 |
| `test_extract_core_*` | 4 | 核心文本提取：有前缀/无前缀/空/仅前缀 |
| `test_parse_range_bounds` | 12 | 条文范围解析：第一条→[1]、第一条至第三条→[1,3] 等 |

---

### 2.3 test_llm.py（17 用例）

| 测试类/函数 | 数量 | 说明 |
|:---|:---:|:---|
| `test_message_*` | 5 | Message 模型：构造/序列化/3 个工厂方法 |
| `test_llm_config_*` | 4 | LLMConfig：默认值/自定义/to_options |
| `test_build_messages_*` | 5 | 消息列表构建：简单/带历史/自定义提示/空提示回退 |
| `test_build_rag_prompt_*` | 2 | RAG prompt 构建：正常/空上下文 |
| `test_llm_identifying_params` | 1 | LangChain 元信息 |
| `test_llm_llm_type` | 1 | _llm_type 属性 |

Mock 策略：`patch("ollama.Client")` 避免网络调用

---

### 2.4 test_embedding.py（7 用例）

| 测试方法 | 说明 |
|:---|:---|
| `test_embed_documents_single_batch` | 单批向量化，验证维度 1024 |
| `test_embed_documents_empty` | 空列表返回 [] |
| `test_embed_query` | 单条查询向量化 |
| `test_batch_splitting` | 20 条文本在 batch_size=8 下正确分批 |
| `test_retry_on_failure` | 前 2 次失败，第 3 次成功 |
| `test_embedder_attributes` | 属性验证 |

Mock 策略：`patch("ollama.Client")` + 自定义 `fake_embed`

---

### 2.5 test_classify.py（30 用例，已有）

| 测试函数 | 数量 | 说明 |
|:---|:---:|:---|
| `test_classify_intent` | 30 | 意图分类：参数化测试（闲聊→False，法律→True） |

---

## 三、测试覆盖分布

```
                 已覆盖      未覆盖
chunking/parser    ✅         LawParser.parse() 需要真实文本文件
chunking/chunker   ✅         _walk_sections 等复杂遍历
rag/engine         ✅         RAGEngine.ask/ask_stream（需 mock LLM + retriever）
rag/retriever      ✅         FAISSRetriever.search（需 mock vector_store）
rag/adjacent       ✅         _expand/_load_map（需 article_map.json）
llm/client         ✅         LawLLM.chat/chat_stream（需 mock ollama）
embedding          ✅         重试细节、异常类型
api                ❌         test_api.py 排除（需 PostgreSQL）
```

---

## 四、Bug 发现

| 编号 | 文件 | 问题 | 状态 |
|:---|:---|:---|:---|
| #1 | `src/rag/hybrid_retriever.py:64` | `search()` 检查全局 `RETRIEVAL_HYBRID_ENABLED` 而非实例 `self._bm25_weight`，导致 batch_eval 传入的权重参数无效 | ✅ 已修复 |
| #2 | `src/rag/adjacent_expander.py:145` | `_parse_range_bounds` 正则 `[一二三四五六七八九十百千]` 不含 "零"，导致含 "零" 的条文范围无法正确解析 | ⚠️ 已知限制 |

---

## 五、运行方式

```bash
# 本地全部单元测试（不含 API 集成测试）
uv run pytest tests/ --ignore=tests/test_api.py -v

# 按模块运行
uv run pytest tests/test_chunking.py -v
uv run pytest tests/test_rag_engine.py -v
uv run pytest tests/test_llm.py -v
uv run pytest tests/test_embedding.py -v

# 含覆盖率报告
uv run pytest tests/ --ignore=tests/test_api.py --cov=src --cov-report=term-missing
```

CI 配置已更新（`.github/workflows/ci.yml`），push/PR 时自动运行全部单元测试。
