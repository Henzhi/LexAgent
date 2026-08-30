"""法名推断软信号加权测试（B2 二阶段）：LawCentroids + LawNameBoostRetriever。

全程注入测试数据，不触达 PG / 真实 embedder。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.rag.law_centroids import LawCentroids, norm_law_name
from src.rag.law_name_boost import LawNameBoostRetriever
from src.rag.retriever import RetrievedDoc

# 两部法律的质心（正交向量，维度 8 便于构造）
V_LABOR = np.array([1, 1, 1, 1, 0, 0, 0, 0], dtype=np.float32)
V_TAX = np.array([0, 0, 0, 0, 1, 1, 1, 1], dtype=np.float32)
ROWS = [
    ("中华人民共和国劳动合同法(2012修正)", V_LABOR),
    ("中华人民共和国税收征收管理法", V_TAX),
]


def _fake_embedder(vec):
    class _E:
        model = "fake-embed"

        def embed_query(self, q):
            return vec

    return _E()


def _fake_base(docs):
    class _B:
        def is_ready(self):
            return True

        def search(self, query, top_k=5, doc_type=None):
            import copy

            return copy.deepcopy(docs)[:top_k]

    return _B()


def _docs():
    return [
        RetrievedDoc(
            content="用人单位应当支付经济补偿。",
            score=0.90,
            law_name="中华人民共和国劳动合同法",
            article_range="第四十六条",
        ),
        RetrievedDoc(
            content="税收保全的规定。",
            score=0.85,
            law_name="中华人民共和国税收征收管理法",
            article_range="第三十八条",
        ),
    ]


@pytest.fixture
def centroids():
    return LawCentroids(rows=ROWS)


class TestLawCentroids:
    def test_build_and_top_laws(self):
        c = LawCentroids(rows=ROWS)
        assert c.is_ready
        top = c.top_laws(V_LABOR, k=1)
        assert top == ["劳动合同法"]

    def test_contains_law_name_gating(self):
        c = LawCentroids(rows=ROWS)
        assert c.contains_law_name("劳动合同法第四十六条是什么") is True
        assert c.contains_law_name("公司辞退我要赔偿吗") is False

    def test_norm_law_name(self):
        assert norm_law_name("中华人民共和国治安管理处罚法(2025修订)") == "治安管理处罚法"


class TestLawNameBoostRetriever:
    def test_boost_promotes_matched_law(self, centroids):
        """无法名查询：命中候选法名的结果 +boost 并重排到前面。"""
        r = LawNameBoostRetriever(
            base_retriever=_fake_base(_docs()),
            embedder=_fake_embedder(V_TAX),
            centroids=centroids,
            boost=0.1,
            top_laws=1,
        )
        out = r.search("公司不开发票怎么举报？")  # 语义偏向税收法
        assert out[0].law_name == "中华人民共和国税收征收管理法"
        assert out[0].score == pytest.approx(0.95)

    def test_gated_when_query_has_law_name(self, centroids):
        """带法名的查询不激活加权（精确路径优先）。"""
        base_docs = _docs()
        r = LawNameBoostRetriever(
            base_retriever=_fake_base(base_docs),
            embedder=_fake_embedder(V_TAX),
            centroids=centroids,
            boost=0.1,
            top_laws=1,
        )
        out = r.search("劳动合同法第四十六条是什么")
        assert [d.score for d in out] == [0.90, 0.85]  # 原序原分

    def test_gated_when_query_has_article_number(self, centroids):
        r = LawNameBoostRetriever(
            base_retriever=_fake_base(_docs()),
            embedder=_fake_embedder(V_TAX),
            centroids=centroids,
            boost=0.1,
            top_laws=1,
        )
        out = r.search("第三十八条讲的是什么")
        assert [d.score for d in out] == [0.90, 0.85]

    def test_top_laws_limits_candidates(self, centroids):
        """top_laws=1 时只有最近邻法获得加权。"""
        r = LawNameBoostRetriever(
            base_retriever=_fake_base(_docs()),
            embedder=_fake_embedder(V_LABOR),
            centroids=centroids,
            boost=0.1,
            top_laws=1,
        )
        out = r.search("公司辞退我要赔偿吗")
        assert out[0].law_name == "中华人民共和国劳动合同法"  # 劳动合同法被加权
        # 税收法未加权（不在 top1 候选内）
        assert out[1].score == 0.85

    def test_failure_returns_original_order(self, centroids):
        """加权组件故障 → 原序返回（横切组件故障不阻断主链路）。"""

        class _Broken:
            model = "broken"

            def embed_query(self, q):
                raise RuntimeError("embedder down")

        base_docs = _docs()
        r = LawNameBoostRetriever(
            base_retriever=_fake_base(base_docs),
            embedder=_Broken(),
            centroids=centroids,
            boost=0.1,
            top_laws=1,
        )
        out = r.search("公司辞退我要赔偿吗")
        assert [d.score for d in out] == [0.90, 0.85]

    def test_zero_score_neighbors_not_boosted(self, centroids):
        """0 分邻居（Adjacent 填充占位）不参与加权。"""
        docs = _docs() + [
            RetrievedDoc(
                content="邻居条文", score=0.0, law_name="中华人民共和国税收征收管理法", article_range="第三十九条"
            ),
        ]
        r = LawNameBoostRetriever(
            base_retriever=_fake_base(docs),
            embedder=_fake_embedder(V_TAX),
            centroids=centroids,
            boost=0.1,
            top_laws=1,
        )
        out = r.search("公司不开发票怎么举报？")
        assert out[-1].score == 0.0  # 邻居保持 0 分
