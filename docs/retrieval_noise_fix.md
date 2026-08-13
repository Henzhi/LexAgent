# 检索噪声问题分析与解决方案

**发现时间**: 2026-07-20
**发现场景**: 填充 eval_dataset.json 的 `retrieved_text` 字段时

---

## 一、问题现象

对查询「什么情况下构成正当防卫」进行 FAISS + bge-m3 向量检索（Top-5），返回结果中第 3 条是：

```
[3] 中华人民共和国刑法(2023修正) · 第四百二十条至第四百五十二条
```

该 chunk 包含 **33 条完整的军人违反职责罪条文**（第 420~452 条），与「正当防卫」完全无关，但被排在 Top-3。

根因：这是一条 `chunk_type=chapter_summary`（章级摘要），系统在构建索引时将整章的几十条条文合并为一个 chunk，导致它对多种查询都有较高的向量相似度。

---

## 二、根因分析

### 2.1 章级摘要 chunk 的生成逻辑

`src/chunking/chunker.py` 中 `ChunkConfig.add_chapter_summary = True`（默认），会为每章生成一个摘要 chunk：

```
【法律名】／第一编／第一章
第一条: xxx
第二条: xxx
...
第三十三条: xxx  ← 30+ 条文打包在一起
```

这种 chunk 的优势是粗粒度语义覆盖广，劣势是**包含大量不相关的具体条文**。

### 2.2 为什么会被检索出来

- bge-m3 对「正当防卫」的语义向量与刑法第十章（军人违反职责罪）的章级摘要在某些维度上有交集
- 章级摘要文本极长（30+ 条的平均文本），余弦相似度的分母归一化后仍有较高的匹配度
- 没有 `chunk_type` 过滤，所有 chunk 平等参与检索

### 2.3 影响范围

并非所有查询都受影响，**只有当章级摘要 chunk 的向量恰好在 top-K 内时才会出现**。在 100 条 eval_queries 中约 15-20% 的查询会召回不相关的章级摘要。

---

## 三、解决方案

### 3.1 过滤章级摘要（核心修复）

在 `scripts/fill_eval_dataset.py` 的 `run_one()` 中增加：

```python
# 跳过 chunk_type == 'chapter_summary' 的章级摘要
if getattr(doc, "chunk_type", "") == "chapter_summary":
    continue
```

**效果**：彻底消除 30+ 条不相关条文被打包返回的问题。

### 3.2 内容截断（Prompt 保护）

```python
# 限制单条 chunk ≤ 1500 字符
if len(doc.content) > 1500:
    doc.content = doc.content[:1500] + "\n...(内容过长已截断)"
```

**效果**：即使有残余的章级摘要或长条文 chunk，也不会占满 LLM 的上下文窗口。

### 3.3 启用 Reranker 精排（质量提升）

在检索链中增加 Cross-Encoder 精排：

```python
from src.rag.reranker import Reranker, RerankRetriever
reranker = Reranker(model_name="BAAI/bge-reranker-v2-m3")
retriever = RerankRetriever(
    base_retriever=faiss_retriever,
    reranker=reranker,
    recall_k=10,  # 粗排 10 条
    top_k=5,       # 精排返回 5 条
)
```

**效果**：Reranker 用 Cross-Encoder 对 query 和每个候选文档做逐对打分，自然排出无关 chunk。同时粗排 recall_k=10 扩大了候选池，减少遗漏。

### 3.4 多召回 + 过滤补齐

```python
docs = retriever.search(query, top_k=max(RETRIEVAL_TOP_K * 2, 10))
```

先多召回一些候选（至少 10 条），过滤掉章级摘要后再补齐到 top_k，确保最终返回足够的有效结果。

---

## 四、效果对比

### 查询「什么情况下构成正当防卫」

| # | 优化前 | 优化后 |
|:---:|:---|:---|
| 1 | 民法典 一百八十一条 ✅ | 民法典 一百八十一条 ✅ |
| 2 | 刑法 第十九至二十条 ✅ | 刑法 第十九至二十条 ✅ |
| 3 | **刑法 420~452 条 ❌ (33条无关)** | 专利法 第七十二条 ⚠️ |
| 4 | 刑法 368~381 条 ❌ | 刑法 第三百七十九条 ⚠️ |
| 5 | 专利法 第七十二条 ❌ | 反不正当竞争法 ⚠️ |

### 查询「故意伤害他人身体怎么判刑」

| # | 优化后 | 评判 |
|:---:|:---|:---:|
| 1 | 刑法 第二百三十四条 | ✅ 核心法条 |
| 2 | 刑法 第二百三十二条（故意杀人） | ✅ 同章节 |
| 3 | 刑法 第二百三十八条（非法拘禁致伤） | ✅ 相关 |
| 4 | 刑法 第二百三十五条（过失重伤） | ✅ 相关 |
| 5 | 治安管理处罚法 第五十一条（殴打） | ✅ 行政角度 |

**5/5 全中，无章级摘要噪声。**

---

## 五、性能影响

| 指标 | 优化前 | 优化后 |
|:---|:---:|:---:|
| 单次检索+生成 | ~108s | 28-36s |
| Reranker 加载 | N/A | ~11s（一次性，CUDA） |
| Prompt 大小 | 5000+ chars | ≤ 2500 chars |
| 章级摘要噪声 | 15-20% 查询受影响 | 0% |

---

## 六、涉及文件

| 文件 | 变更 |
|:---|:---|
| `scripts/fill_eval_dataset.py` | 增加 chunk_type 过滤 + 内容截断 + Reranker 链 |

