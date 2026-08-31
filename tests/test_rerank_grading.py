"""rerank 分级召回（P1b）+ 防呆告警（P2）+ 输入截断（P1a）测试。

背景见 docs/检索质量与响应性能评估-2026-08-31.md §5：
- 「法名+第X条」查询由 ArticleRouter 精确置顶，rerank 对其召回无增益，跳过省 ~1.3s；
- recall_k <= top_k 会让 rerank 静默跳过（历史坑：15==15 生产长期未生效），须告警；
- CrossEncoder 耗时对文本长度 O(n²)，打分输入需截断保护。
"""

from __future__ import annotations

import logging

from src.rag.article_router import extract_law_hint, is_article_routed_query
from src.rag.reranker import Reranker, RerankRetriever
from src.rag.retriever import RetrievedDoc


class TestIsArticleRoutedQuery:
    def test_law_name_plus_article(self):
        assert is_article_routed_query("治安管理处罚法第十条")
        assert is_article_routed_query("《劳动合同法》第四十六条")
        assert is_article_routed_query("刑法第二十条关于正当防卫的规定")

    def test_article_without_law_hint(self):
        """只有条号、无 法名线索 → ArticleRouter 无法置顶，不得跳过 rerank。"""
        assert not is_article_routed_query("第三十八条是什么")

    def test_plain_colloquial(self):
        assert not is_article_routed_query("加班工资怎么计算")

    def test_empty_and_none(self):
        assert not is_article_routed_query("")
        assert not is_article_routed_query(None)


class TestExtractLawHint:
    def test_book_title_priority(self):
        assert extract_law_hint("《劳动合同法》第四十六条") == "劳动合同法"

    def test_law_suffix(self):
        assert extract_law_hint("刑法第二十条") == "刑法"

    def test_none_for_colloquial(self):
        assert extract_law_hint("加班工资怎么计算") is None


class _SpyBase:
    """记录 search 调用参数的替身。"""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def search(self, query, top_k=5, **kwargs):
        self.calls.append((query, top_k))
        return [
            RetrievedDoc(content=f"c{i}", score=0.9, law_name="刑法", article_range=f"第{i}条") for i in range(top_k)
        ]

    def is_ready(self):
        return True


class _SpyReranker:
    def __init__(self):
        self.calls = 0

    def rerank(self, query, docs, top_k=5):
        self.calls += 1
        return docs[:top_k]


class TestRerankRetrieverGrading:
    def test_article_query_skips_rerank(self):
        """「法名+第X条」查询跳过 rerank，直接向量检索 effective_k 条。"""
        base, rk = _SpyBase(), _SpyReranker()
        r = RerankRetriever(base_retriever=base, reranker=rk, recall_k=40, top_k=15)
        r.search("刑法第二十条", top_k=5)
        assert rk.calls == 0
        assert base.calls == [("刑法第二十条", 5)]

    def test_plain_query_uses_rerank(self):
        """普通查询走完整链：粗排取 recall_k 条再精排。"""
        base, rk = _SpyBase(), _SpyReranker()
        r = RerankRetriever(base_retriever=base, reranker=rk, recall_k=40, top_k=15)
        r.search("加班工资怎么计算", top_k=5)
        assert rk.calls == 1
        assert base.calls == [("加班工资怎么计算", 40)]


class TestRerankRetrieverGuard:
    def test_warn_when_recall_le_topk(self, caplog):
        """recall_k <= top_k 必须打告警（防 rerank 静默跳过的历史坑复发）。"""
        with caplog.at_level(logging.WARNING, logger="src.rag.reranker"):
            RerankRetriever(base_retriever=_SpyBase(), reranker=_SpyReranker(), recall_k=15, top_k=15)
        assert any("recall_k(15) <= top_k(15)" in r.message for r in caplog.records)

    def test_no_warn_when_recall_gt_topk(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.rag.reranker"):
            RerankRetriever(base_retriever=_SpyBase(), reranker=_SpyReranker(), recall_k=40, top_k=15)
        assert not any("recall_k" in r.message for r in caplog.records)


class TestRerankTruncation:
    def test_long_content_truncated_for_scoring_only(self):
        """超长 content 只截打分输入，返回的 doc 保持原文。"""
        import src.rag.reranker as rr

        captured: dict = {}

        class FakeCrossEncoder:
            def predict(self, pairs, show_progress_bar=False):
                captured["lens"] = [len(p[1]) for p in pairs]
                return [0.5] * len(pairs)

        r = Reranker.__new__(Reranker)  # 跳过 __init__，不加载真模型
        r._model = FakeCrossEncoder()

        long_text = "甲" * 3000
        docs = [RetrievedDoc(content=long_text, score=0.9) for _ in range(10)]
        out = r.rerank("查询", docs, top_k=5)
        assert max(captured["lens"]) == rr.RERANK_MAX_CHARS
        assert all(len(d.content) == 3000 for d in out)
