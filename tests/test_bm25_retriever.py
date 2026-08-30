"""BM25 分词与停用词回归测试。

背景：停用词表曾误删「万元」（金额类查询的关键 token），另含从未生效的
死代码「年月日」（jieba 不会把日期切出该整体）。这两个问题都极隐蔽——
索引能建、查询能跑，只是「某些问题搜不到」，因此必须有用例守住。
"""

from __future__ import annotations

import pytest

from src.rag.bm25_retriever import (
    LAW_NAME_BOOST,
    Bm25Retriever,
    _STOPWORDS,
    _is_fragment,
    _tokenize,
)


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
        assert _tokenize("中华人民共和国劳动合同法第四十六条") == _tokenize("劳动合同法第四十六条")

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

        store = _FakeStore(
            [
                ("用人单位应当按月支付劳动报酬", {"law_name": "中华人民共和国劳动法"}),
                ("正当防卫不负刑事责任", {"law_name": "中华人民共和国刑法"}),
            ]
        )
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


class TestFragmentFilter:
    """碎片 chunk 过滤：纯条号引用（「第九条」「第十七条、」）不得进索引。

    基线评测发现 ~3400 个此类碎片（占 6.4%），DF 极高且文档极短在 BM25
    长度归一化下占便宜，会系统性挤占 top-k。
    """

    def test_article_number_only_is_fragment(self):
        assert _is_fragment("第九条")
        assert _is_fragment("第十七条、")
        assert _is_fragment("第二十条、第二十一条")

    def test_substantive_text_is_not_fragment(self):
        assert not _is_fragment("用人单位应当按月支付劳动报酬")
        assert not _is_fragment("正当防卫不负刑事责任")

    def test_load_index_skips_fragments(self):
        store = _FakeStore(
            [
                ("第九条", {"law_name": "刑法", "article_range": "第九条"}),
                ("正当防卫不负刑事责任", {"law_name": "刑法", "article_range": "第二十条"}),
            ]
        )
        retriever = Bm25Retriever(store)
        retriever.load_index()
        assert len(retriever._chunks) == 1


class TestLawNameBoost:
    """法名加权：法名 token 在本法 chunk 内 TF 应等于 LAW_NAME_BOOST。

    法名只拼一次时 DF=该法 chunk 数，IDF 被稀释到低于条号 token（实测
    「刑法」3.53 < 「第二十条」4.11），导致「刑法第二十条」被其他法律的
    第二十条淹没。文档内重复不改变 BM25 的 DF，是纯增益的加权方式。
    """

    def test_law_name_token_repeated_in_index(self):
        store = _FakeStore(
            [
                ("正当防卫不负刑事责任", {"law_name": "中华人民共和国刑法"}),
            ]
        )
        retriever = Bm25Retriever(store)
        retriever.load_index()
        doc_terms = retriever._bm25.doc_freqs[0]
        assert doc_terms.get("刑法", 0) == LAW_NAME_BOOST

    def test_law_name_beats_other_laws_same_article(self):
        """「刑法第二十条」应命中刑法，而不是其他法律的第二十条。

        回归基线：加权前 top5 全是信托法/关税法等的「第二十条」碎片。
        """
        store = _FakeStore(
            [
                ("正当防卫不负刑事责任", {"law_name": "中华人民共和国刑法", "article_range": "第二十条"}),
                ("信托当事人的其他权利义务", {"law_name": "中华人民共和国信托法", "article_range": "第二十条"}),
                ("关税的退还与补缴规则", {"law_name": "中华人民共和国关税法", "article_range": "第二十条"}),
            ]
        )
        retriever = Bm25Retriever(store)
        retriever.load_index()
        docs = retriever.search("刑法第二十条", top_k=3)
        assert docs[0].law_name == "中华人民共和国刑法"


class TestSearchDedupByArticle:
    """同一条文的多 chunk 只保留最高排名的一个，空位由后续条文补足。"""

    def test_same_article_chunks_dedup(self):
        store = _FakeStore(
            [
                ("第四十二条第一款关于初步审查的内容", {"law_name": "专利法实施细则", "article_range": "第四十二条"}),
                ("第四十二条第二款关于期限补偿的内容", {"law_name": "专利法实施细则", "article_range": "第四十二条"}),
                ("第四十三条关于优先权恢复的内容", {"law_name": "专利法实施细则", "article_range": "第四十三条"}),
            ]
        )
        retriever = Bm25Retriever(store)
        retriever.load_index()
        docs = retriever.search("第四十二条 第四十三条 初步审查 期限补偿", top_k=5)
        keys = [(d.law_name, d.article_range) for d in docs]
        assert len(keys) == len(set(keys))
        assert len(docs) == 2  # 去重后空位被第四十三条补足，不浪费名额

    def test_chunks_without_article_range_not_dedup(self):
        """article_range 为空的 chunk（总则等）不参与去重。"""
        store = _FakeStore(
            [
                ("总则编的适用范围说明", {"law_name": "民法典", "article_range": ""}),
                ("总则编的基本原则说明", {"law_name": "民法典", "article_range": ""}),
            ]
        )
        retriever = Bm25Retriever(store)
        retriever.load_index()
        docs = retriever.search("总则编 段 内容", top_k=5)
        assert len(docs) == 2
