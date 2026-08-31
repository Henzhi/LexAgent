"""
LLM 查询改写 + 双路 RRF 融合（无法名口语查询的正交信号，B2 后续）。

为什么这是正交信号（对照 law_name_boost 的 NO-GO 教训，见
docs/B2-法名推断spike报告-2026-08-30.md §6）：改写注入的是 **LLM 的法律
知识**（口语→法言法语的语域映射、适用法律判断），是 bge-m3 不掌握的信息；
而质心加权只是同源向量再聚合，零正交增益纯注入噪声。

设计（软信号铁律的延续）：
- **双路融合而非替换**：原查询 + 改写查询各跑一遍完整检索链，RRF 融合——
  改写误导时原路保底，改写有效时改写路增益；
- 仅无 法名/条号的查询触发（带法名走精确路径，precise 97.5% 不动）；
- 改写失败、或改写与原句相同 → 退化为单路，零伤害；
- 改写提示词**允许在确信时补法名**（借力 precise 路径强度），但严禁编造
  条款号；原句中的法条引用（第X条/《法律名》）必须在改写结果中保留
  （护栏复用 src/agents/rewrite.py 的规则）；
- 融合按 RRF 排名排序，**不改写 doc.score**（保留 rerank 原始分数尺度，
  下游阈值/展示语义不变）。

成本：仅触发查询 +1 次 LLM 调用（走 LLMBackend 公开入口，预算 callback
正常计数）+1 路检索。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from src.rag.law_centroids import LawCentroids, norm_law_name
from src.rag.retriever import BaseRetriever

logger = logging.getLogger(__name__)

_ARTICLE_RE = re.compile(r"第[零一二两三四五六七八九十百千\d]+条")
# 原句法条引用护栏（与 src/agents/rewrite.py 同规则）
_ARTICLE_REF = re.compile(r"第[0-9０-９零一二三四五六七八九十百千两]+条")
_LAW_REF = re.compile(r"《[^》]+》")

_FUSION_PROMPT = """你是一名"法律检索查询改写助手"。用户用口语描述法律问题，检索系统需要规范的法言法语。请把问题改写为**规范的法律检索查询**。

改写规则：
1. 把口语映射为对应法律概念/案由/罪名（"被公司开了"→"违法解除劳动合同"）。
2. 若能较高把握判断适用的法律，可在改写中加入该法律简称（如"劳动合同法"）；不确定就不要加。
3. 严禁编造条款号；严禁改变原意；严禁新增用户未提及的法律关系或情节。
4. 只输出改写后的查询本身，禁止解释和引号。

示例：
用户：公司辞退我要赔偿吗
改写：劳动合同法 用人单位解除劳动合同的经济补偿

用户：在网上泄露国家秘密会坐牢吗
改写：泄露国家秘密的刑事责任

用户：{query}"""


class RewriteFusionRetriever(BaseRetriever):
    """检索器装饰器（最外层）：无法名查询 → LLM 改写 → 双路检索 RRF 融合。

    Args:
        base_retriever: 内层完整检索链（含 rerank/adjacent/hybrid）
        llm: LLMBackend（复用 rewrite_query 同款公开入口；预算 callback 已挂载）
        centroids: LawCentroids（用于「查询含已知法名」门控；可注入测试替身）
        recall_k: 每路检索返回条数（融合前的候选深度）
        rrf_k: RRF 常数（标准 60）
    """

    def __init__(
        self,
        base_retriever: BaseRetriever,
        llm: Any,
        centroids: LawCentroids,
        recall_k: int = 20,
        rrf_k: int = 60,
    ):
        self._base = base_retriever
        self._llm = llm
        self._centroids = centroids
        self._recall_k = max(5, int(recall_k))
        self._rrf_k = int(rrf_k)

    def is_ready(self) -> bool:
        return self._base.is_ready()

    def search(self, query: str, top_k: int = 5, doc_type: str | None = None) -> list:
        base_docs = self._base.search(query, top_k=self._recall_k, doc_type=doc_type)
        try:
            rewritten = self._rewrite_if_applicable(query)
        except Exception as e:
            logger.warning(f"改写失败（单路返回）: {e}")
            rewritten = ""
        if not rewritten or rewritten.strip() == (query or "").strip():
            return base_docs[:top_k]
        new_docs = self._base.search(rewritten, top_k=self._recall_k, doc_type=doc_type)
        fused = _rrf_fuse([base_docs, new_docs], rrf_k=self._rrf_k)
        return fused[:top_k]

    def _rewrite_if_applicable(self, query: str) -> str:
        """门控（与 law_name_boost 同款）+ 改写。返回空串 = 不改写。"""
        if not query or not query.strip():
            return ""
        if _ARTICLE_RE.search(query) or self._centroids.contains_law_name(query):
            return ""  # 带法名/条号 → 精确路径，不干预
        self._centroids.ensure_loaded()
        out = _rewrite_with_law_hint(self._llm, query)
        return out if out and out.strip() != query.strip() else ""


def _rewrite_with_law_hint(llm: Any, query: str) -> str:
    """融合路专用改写：允许确信时补法名；保留原句法条引用；失败回退原句。"""
    try:
        out = (llm.chat(_FUSION_PROMPT.format(query=query)) or "").strip()
    except Exception as e:
        logger.warning(f"改写调用失败: {e}")
        return query
    if len(out) >= 2 and out[0] in "\"“'‘" and out[-1] in "\"”'’":
        out = out[1:-1].strip()
    for prefix in ("改写：", "改写:", "改写", "回答：", "回答:", "结果：", "结果:"):
        if out.startswith(prefix):
            out = out[len(prefix) :].strip()
            break
    out = " ".join(out.split())
    if not out:
        return query
    # 护栏：原句中的法条引用丢失 → 视为改变检索意图，回退原句
    refs = _ARTICLE_REF.findall(query) + _LAW_REF.findall(query)
    if refs and any(ref not in out for ref in refs):
        logger.warning("改写丢失法条引用 %s（%r → %r），回退原句", refs, query, out)
        return query
    return out


def _doc_key(doc: Any) -> tuple:
    g = (lambda k: doc.get(k, "")) if isinstance(doc, dict) else (lambda k: getattr(doc, k, "") or "")
    return (norm_law_name(g("law_name")), g("article_range"), (g("content") or "")[:60])


def _rrf_fuse(doc_lists: list[list], rrf_k: int = 60) -> list:
    """加权 RRF：按排名融合（不碰分数，doc.score 保持各路原始值）。"""
    scores: dict[tuple, float] = {}
    by_key: dict[tuple, Any] = {}
    for docs in doc_lists:
        for rank, doc in enumerate(docs):
            key = _doc_key(doc)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            by_key.setdefault(key, doc)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [by_key[key] for key, _ in ranked]


def make_default_llm() -> Any:
    """融合路的改写 LLM（DeepSeek 主模型，预算 callback 由工厂挂载）。"""
    from src.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
    from src.llm.factory import create_llm_backend

    return create_llm_backend(
        backend_type="openai",
        model=OPENAI_MODEL,
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL,
        max_tokens=128,
    )


# Callable[[str], str] 形式的轻量改写器（如需绕过完整 backend 的场景）
Rewriter = Callable[[str], str]
