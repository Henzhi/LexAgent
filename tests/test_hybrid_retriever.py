"""HybridRetriever 条件激活与融合行为测试。

2026-08-29 向量路排查发现：w=3.0 时 BM25 词面排序在「法名+语义」查询上
碾压向量排名（语义集净丢 6 条），权重已重定为 0.5。另实测「BM25 常开」
在两集上均不如条件激活（语义 -6、法条级 -1.2），故 always_on 默认关闭。
这些结论依赖激活判据与融合公式的行为稳定，必须有测试守住。
"""

from __future__ import annotations

import pytest

from src.rag.hybrid_retriever import HybridRetriever


class _Doc:
    def __init__(self, law_name, article_range):
        self.law_name = law_name
        self.article_range = article_range


class _FakeBase:
    """向量路替身：返回固定 3 条，顺序即向量排名。"""

    def __init__(self, docs=None):
        self._docs = docs or [
            _Doc("中华人民共和国刑法", "第二十条"),
            _Doc("中华人民共和国刑法", "第二百三十六条"),
            _Doc("中华人民共和国民法典", "第三百六十七条"),
        ]

    def is_ready(self):
        return True

    def search(self, query, top_k=5, **kwargs):
        return self._docs[:top_k]


class _FakeBM25:
    """BM25 替身：记录被调用次数，返回固定顺序（故意与向量不同序）。"""

    def __init__(self, docs=None):
        self._docs = docs or [
            _Doc("中华人民共和国刑法", "第二百三十六条"),
            _Doc("中华人民共和国刑法", "第二十条"),
            _Doc("中华人民共和国专利法", "第四十二条"),
        ]
        self.calls: list[str] = []

    def search(self, query, top_k=5, **kwargs):
        self.calls.append(query)
        return self._docs[:top_k]


class TestShouldActivate:
    """条件激活：只有查询含法名/条款号时 BM25 才参与。"""

    def test_article_number_query_activates(self):
        bm = _FakeBM25()
        r = HybridRetriever(_FakeBase(), bm, bm25_weight=0.5)
        r.search("刑法第二十条")
        assert bm.calls == ["刑法第二十条"]

    def test_book_title_query_activates(self):
        bm = _FakeBM25()
        r = HybridRetriever(_FakeBase(), bm, bm25_weight=0.5)
        r.search("《民法典》关于居住权")
        assert len(bm.calls) == 1

    def test_pure_semantic_query_not_activated(self):
        """纯语义查询不激活——BM25 词面信号在此类查询上会挤压向量排序。"""
        bm = _FakeBM25()
        r = HybridRetriever(_FakeBase(), bm, bm25_weight=0.5)
        r.search("公司股东之间闹矛盾怎么处理")
        assert bm.calls == []


class TestAlwaysOn:
    """always_on=True 时跳过实体识别，BM25 无条件参与（默认关闭）。"""

    def test_always_on_activates_semantic_query(self):
        bm = _FakeBM25()
        r = HybridRetriever(_FakeBase(), bm, bm25_weight=0.5, always_on=True)
        r.search("公司股东之间闹矛盾怎么处理")
        assert len(bm.calls) == 1

    def test_default_is_conditional(self):
        bm = _FakeBM25()
        r = HybridRetriever(_FakeBase(), bm)  # always_on 默认 False
        r.search("公司股东之间闹矛盾怎么处理")
        assert bm.calls == []


class TestFusion:
    """加权 RRF 融合：只看排名不碰分数，权重决定 BM25 影响力。"""

    def _top1(self, weight: float) -> tuple[str, str]:
        r = HybridRetriever(_FakeBase(), _FakeBM25(), bm25_weight=weight)
        docs = r.search("刑法第二十条", top_k=5)
        return (docs[0].law_name, docs[0].article_range)

    def test_low_weight_keeps_vector_rank_first(self):
        """w=0.5（生产值）：向量 rank1(1/61) 压过 BM25 rank1(0.5/61)。"""
        assert self._top1(0.5) == ("中华人民共和国刑法", "第二十条")

    def test_high_weight_lets_bm25_override_vector(self):
        """w=3.0（旧值）：BM25 rank1 反超向量 rank1——正是排查出的病灶。"""
        assert self._top1(3.0) == ("中华人民共和国刑法", "第二百三十六条")

    @pytest.mark.parametrize("weight", [0.5, 1.0, 3.0])
    def test_fusion_keeps_all_candidates(self, weight):
        """融合只看排名，不应丢条目（两路并集）。"""
        r = HybridRetriever(_FakeBase(), _FakeBM25(), bm25_weight=weight)
        docs = r.search("刑法第二十条", top_k=10)
        keys = {(d.law_name, d.article_range) for d in docs}
        # 向量 3 条 ∪ BM25 3 条，其中 2 条重叠 → 并集 4 条
        assert len(keys) == 4
