"""LLM 改写双路融合测试（B2 后续）：RewriteFusionRetriever。

注入 fake base/llm/centroids，不触达 PG 与真实 LLM。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.rag.law_centroids import LawCentroids
from src.rag.rewrite_fusion import RewriteFusionRetriever, _rrf_fuse
from src.rag.retriever import RetrievedDoc

ROWS = [("中华人民共和国劳动合同法", np.array([1, 1, 0, 0], dtype=np.float32))]

_LABOR_DOC = RetrievedDoc(
    content="用人单位解除劳动合同应当支付经济补偿。", score=0.9,
    law_name="中华人民共和国劳动合同法", article_range="第四十六条",
)
_CIVIL_DOC = RetrievedDoc(
    content="违约责任的一般规定。", score=0.8,
    law_name="中华人民共和国民法典", article_range="第五百七十七条",
)


class _FakeBase:
    """按查询返回脚本化结果的假检索链，记录每次搜索的查询。"""

    def __init__(self, responses: dict[str, list]):
        self.responses = responses
        self.searched: list[str] = []

    def is_ready(self):
        return True

    def search(self, query, top_k=5, doc_type=None):
        self.searched.append(query)
        import copy

        return copy.deepcopy(self.responses.get(query, []))[:top_k]


class _FakeLLM:
    def __init__(self, out: str | Exception):
        self.out = out
        self.prompts: list[str] = []

    def chat(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def _fusion(llm_out, base: _FakeBase, rows=ROWS):
    return RewriteFusionRetriever(
        base_retriever=base, llm=_FakeLLM(llm_out),
        centroids=LawCentroids(rows=rows), recall_k=5, rrf_k=60,
    )


class TestRewriteFusion:
    def test_dual_path_fuses_rewrite_only_doc(self):
        """无法名查询：改写路独有的条文进入融合结果（正交增益的最小验证）。"""
        base = _FakeBase({
            "公司辞退我要赔偿吗": [_LABOR_DOC],
            "劳动合同法 用人单位解除劳动合同的经济补偿": [_CIVIL_DOC],
        })
        out = _fusion("劳动合同法 用人单位解除劳动合同的经济补偿", base).search("公司辞退我要赔偿吗", top_k=5)
        assert base.searched == ["公司辞退我要赔偿吗", "劳动合同法 用人单位解除劳动合同的经济补偿"]
        assert {d.law_name for d in out} == {"中华人民共和国劳动合同法", "中华人民共和国民法典"}

    def test_rerank_score_preserved(self):
        """RRF 只定序不改分：doc.score 保留各路原始 rerank 分数。"""
        base = _FakeBase({
            "q": [_LABOR_DOC],
            "q改": [_CIVIL_DOC],
        })
        out = _fusion("q改", base).search("q", top_k=5)
        assert [d.score for d in out] == [0.9, 0.8]  # 原分数不被 RRF 覆盖

    def test_same_rewrite_degenerates_to_single_path(self):
        """改写与原句相同 → 退化为单路（base 只被调一次）。"""
        base = _FakeBase({"q": [_LABOR_DOC]})
        out = _fusion("q", base).search("q", top_k=5)
        assert base.searched == ["q"]
        assert [d.law_name for d in out] == ["中华人民共和国劳动合同法"]

    def test_gated_when_query_has_law_name(self):
        base = _FakeBase({"劳动合同法第四十六条": [_LABOR_DOC]})
        out = _fusion("随便", base).search("劳动合同法第四十六条", top_k=5)
        assert base.searched == ["劳动合同法第四十六条"]  # 单路，不调 LLM

    def test_gated_when_query_has_article_number(self):
        base = _FakeBase({"第三十八条是什么": [_LABOR_DOC]})
        _fusion("随便", base).search("第三十八条是什么", top_k=5)
        assert base.searched == ["第三十八条是什么"]

    def test_llm_failure_falls_back_to_single_path(self):
        base = _FakeBase({"q": [_LABOR_DOC]})
        out = _fusion(RuntimeError("llm down"), base).search("q", top_k=5)
        assert base.searched == ["q"]
        assert len(out) == 1

    def test_ref_guard_reverts_when_article_ref_lost(self):
        """原句含《法名》而改写丢失 → 回退原句（单路）。"""
        base = _FakeBase({"《劳动合同法》第四十六条": [_LABOR_DOC]})
        out = _fusion("故意丢掉引用的改写", base).search("《劳动合同法》第四十六条", top_k=5)
        assert base.searched == ["《劳动合同法》第四十六条"]

    def test_rrf_fuse_dedup_and_order(self):
        a = [RetrievedDoc(content="c1", score=0.9, law_name="法A", article_range="第一条"),
             RetrievedDoc(content="c2", score=0.8, law_name="法B", article_range="第二条")]
        b = [RetrievedDoc(content="c2", score=0.7, law_name="法B", article_range="第二条"),
             RetrievedDoc(content="c3", score=0.6, law_name="法C", article_range="第三条")]
        out = _rrf_fuse([a, b], rrf_k=60)
        # c2 两路都出现 → RRF 最高；c1/c3 各单路一次，rank 靠前者略高
        assert [d.content for d in out] == ["c2", "c1", "c3"]
        assert len(out) == 3
