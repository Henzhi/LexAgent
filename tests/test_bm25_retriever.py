"""BM25 分词与停用词回归测试。

背景：停用词表曾误删「万元」（金额类查询的关键 token），另含从未生效的
死代码「年月日」（jieba 不会把日期切出该整体）。这两个问题都极隐蔽——
索引能建、查询能跑，只是「某些问题搜不到」，因此必须有用例守住。
"""
from __future__ import annotations

import pytest

from src.rag.bm25_retriever import _STOPWORDS, _tokenize


class TestStopwordTable:
    """停用词表本身：只放真正的虚词。"""

    def test_amount_and_date_units_not_stopwords(self):
        """「万元」「年月日」不得出现在停用词表里（历史 Bug 回归）。"""
        assert "万元" not in _STOPWORDS
        assert "年月日" not in _STOPWORDS

    def test_function_words_are_stopwords(self):
        assert {"的", "了", "和", "是", "根据", "依照"} <= _STOPWORDS

    def test_modal_verbs_kept_pending_evaluation(self):
        """法律模态词有语义价值，是否过滤应由评测决定，当前不过滤。"""
        assert "不得" not in _STOPWORDS
        assert "应当" not in _STOPWORDS
        assert "可以" not in _STOPWORDS


class TestTokenize:
    def test_amount_query_keeps_unit(self):
        """金额类查询必须保留计量单位，否则丢失区分度。"""
        tokens = _tokenize("工伤死亡赔偿金大概是多少万元")
        assert "万元" in tokens

    def test_date_text_not_truncated(self):
        """日期按 jieba 切分保留，「年月日」这类死停用词不得引入。"""
        tokens = _tokenize("本法自2023年1月1日起施行")
        assert "年月日" not in tokens
        assert "2023" in tokens

    def test_function_words_filtered(self):
        assert _tokenize("的 了 和 根据 依照") == []

    def test_empty_text(self):
        assert _tokenize("") == []
        assert _tokenize(None) == []

    def test_punctuation_stripped(self):
        assert _tokenize("《刑法》第二十条：正当防卫") == ["刑法", "第二十条", "正当防卫"]

    def test_law_name_prefix_normalized(self):
        """法名前缀归一化：带不带「中华人民共和国」应等价。"""
        assert _tokenize("中华人民共和国劳动合同法第四十六条") == _tokenize(
            "劳动合同法第四十六条"
        )

    @pytest.mark.parametrize("query", ["赔偿多少万元", "罚款十万元"])
    def test_amount_queries_produce_tokens(self, query):
        assert _tokenize(query)


class _FakeStore:
    """最小 store 替身：只提供 Bm25Retriever.load_index 需要的两个方法。"""

    def __init__(self, rows):
        self._rows = rows

    def ensure_tables(self):
        pass

    def fetch_all_active_chunks(self):
        return self._rows


class TestBm25RetrieverSearch:
    def test_law_name_merged_into_index_text(self):
        """法名拼入索引文本，使「法名+关键词」可被 BM25 精确命中。"""
        from src.rag.bm25_retriever import Bm25Retriever

        store = _FakeStore([
            ("用人单位应当按月支付劳动报酬", {"law_name": "中华人民共和国劳动法"}),
            ("正当防卫不负刑事责任", {"law_name": "中华人民共和国刑法"}),
        ])
        retriever = Bm25Retriever(store)
        retriever.load_index()

        docs = retriever.search("劳动法 劳动报酬", top_k=5)
        assert docs
        assert docs[0].law_name == "中华人民共和国劳动法"

    def test_all_stopword_query_returns_empty(self):
        """纯虚词查询不应抛异常，返回空列表由向量检索兜底。"""
        from src.rag.bm25_retriever import Bm25Retriever

        store = _FakeStore([("用人单位应当按月支付劳动报酬", {"law_name": "劳动法"})])
        retriever = Bm25Retriever(store)
        retriever.load_index()

        assert retriever.search("的 了 和", top_k=5) == []
