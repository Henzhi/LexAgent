"""chunking 模块单元测试 — parser 纯函数 + chunker 纯函数"""

import pytest
from src.chunking.parser import Article, Chapter, Section, Part, LawDocument, _cn_to_int
from src.chunking.chunker import (
    _build_article_context,
    _build_context_prefix,
    _make_article_range_text,
    ChunkConfig,
)
from src.rag.retriever import RetrievedDoc, doc_to_retrieved
from langchain_core.documents import Document


# ============================================================
# _cn_to_int — 中文数字转整数
# ============================================================

@pytest.mark.parametrize("cn,expected", [
    ("一", 1),
    ("二", 2),
    ("三", 3),
    ("四", 4),
    ("五", 5),
    ("六", 6),
    ("七", 7),
    ("八", 8),
    ("九", 9),
    ("十", 10),
    ("十一", 11),
    ("十二", 12),
    ("二十", 20),
    ("二十一", 21),
    ("三十", 30),
    ("九十九", 99),
    ("一百", 100),
    ("一百零一", 101),
    ("一百一十", 110),
    ("一百二十三", 123),
    ("二百", 200),
    ("九百九十九", 999),
    ("一千", 1000),
    ("一千零一", 1001),
    ("一千二百三十四", 1234),
])
def test_cn_to_int_valid(cn, expected):
    assert _cn_to_int(cn) == expected


def test_cn_to_int_zero():
    assert _cn_to_int("零") == 0


def test_cn_to_int_empty():
    assert _cn_to_int("") == 0


# ============================================================
# LawDocument 数据模型
# ============================================================

def test_law_document_basic():
    doc = LawDocument(file_path="test.txt", title="测试法")
    assert doc.title == "测试法"
    assert doc.articles == []
    assert doc.chapters == []
    assert doc.parts == []


def test_law_document_with_hierarchy():
    article = Article(index=1, number="一", number_int=1, text="第一条内容")
    section = Section(title="第一节", articles=[article])
    chapter = Chapter(title="第一章", sections=[section], articles=[article])
    part = Part(title="第一编", chapters=[chapter])
    doc = LawDocument(
        file_path="test.txt", title="测试法",
        preamble="序言", parts=[part],
        chapters=[chapter], articles=[article],
    )
    assert len(doc.parts) == 1
    assert len(doc.chapters) == 1
    assert len(doc.articles) == 1
    assert doc.articles[0].number == "一"
    assert doc.parts[0].chapters[0].sections[0].title == "第一节"


# ============================================================
# Article / Section / Chapter / Part 数据模型
# ============================================================

def test_article_model():
    a = Article(index=5, number="五", number_int=5, text="正文", content="完整内容")
    assert a.index == 5
    assert a.number == "五"
    assert a.number_int == 5
    assert a.text == "正文"
    assert a.content == "完整内容"


def test_section_model():
    a = Article(index=1, number="一", number_int=1, text="test")
    s = Section(title="第一节", articles=[a])
    assert s.title == "第一节"
    assert len(s.articles) == 1
    assert s.articles[0].number == "一"


def test_chapter_with_articles():
    a = Article(index=1, number="一", number_int=1, text="test")
    ch = Chapter(title="第一章", articles=[a])
    assert ch.title == "第一章"
    assert len(ch.articles) == 1
    assert ch.sections == []


# ============================================================
# _build_article_context — 上下文元数据
# ============================================================

def test_build_article_context():
    doc = LawDocument(file_path="test.txt", title="测试法律")
    article = Article(index=3, number="三", number_int=3, text="第三条内容")
    ctx = _build_article_context(
        doc, article,
        chapter_title="第一章", section_title="第一节", part_title="第一编",
    )
    assert ctx["law_name"] == "测试法律"
    assert ctx["part"] == "第一编"
    assert ctx["chapter"] == "第一章"
    assert ctx["section"] == "第一节"
    assert ctx["article_number"] == "三"
    assert ctx["article_number_int"] == "3"
    assert ctx["article_index"] == "3"


def test_build_article_context_no_part():
    doc = LawDocument(file_path="test.txt", title="某法")
    article = Article(index=1, number="一", number_int=1, text="第一条")
    ctx = _build_article_context(doc, article)
    assert ctx["part"] == ""
    assert ctx["chapter"] == ""
    assert ctx["section"] == ""


# ============================================================
# _build_context_prefix — 上下文前缀
# ============================================================

def test_build_context_prefix_full():
    meta = {
        "law_name": "中华人民共和国刑法",
        "part": "第二编",
        "chapter": "第一章",
        "section": "第一节",
    }
    cfg = ChunkConfig()
    prefix = _build_context_prefix(meta, cfg)
    assert "中华人民共和国刑法" in prefix
    assert "第二编" in prefix
    assert "第一章" in prefix
    assert "第一节" in prefix


def test_build_context_prefix_minimal():
    meta = {"law_name": "某法", "part": "", "chapter": "", "section": ""}
    cfg = ChunkConfig()
    prefix = _build_context_prefix(meta, cfg)
    assert "某法" in prefix
    assert "/" not in prefix or prefix.count("／") == 0  # 无额外分隔符


# ============================================================
# _make_article_range_text — 条文范围
# ============================================================

def test_article_range_single():
    articles = [Article(index=1, number="一", number_int=1, text="")]
    result = _make_article_range_text(articles)
    assert result == "第一条"


def test_article_range_multi():
    articles = [
        Article(index=1, number="一", number_int=1, text=""),
        Article(index=2, number="二", number_int=2, text=""),
        Article(index=3, number="三", number_int=3, text=""),
    ]
    result = _make_article_range_text(articles)
    assert result == "第一条至第三条"


def test_article_range_two_articles():
    articles = [
        Article(index=10, number="十", number_int=10, text=""),
        Article(index=11, number="十一", number_int=11, text=""),
    ]
    result = _make_article_range_text(articles)
    assert result == "第十条至第十一条"


# ============================================================
# ChunkConfig 配置
# ============================================================

def test_chunk_config_defaults():
    cfg = ChunkConfig()
    assert cfg.min_chunk_chars == 50
    assert cfg.max_chunk_chars == 1500
    assert cfg.merge_short_articles is False  # 默认不合并：条文独立成块，可定位到具体条文
    assert cfg.add_chapter_summary is True


def test_chunk_config_custom():
    cfg = ChunkConfig(min_chunk_chars=100, merge_short_articles=False, add_chapter_summary=False)
    assert cfg.min_chunk_chars == 100
    assert not cfg.merge_short_articles
    assert not cfg.add_chapter_summary


def test_articles_are_not_merged_by_default():
    """回归：默认不合并短条文，每条独立成块（召回可定位到具体条文）"""
    from src.chunking.chunker import LawChunker
    doc = LawDocument(
        file_path="test.txt",
        title="测试法",
        parts=[],
        chapters=[],
        articles=[
            Article(index=1, number="一", number_int=1, text="很短", content="很短"),
            Article(index=2, number="二", number_int=2, text="也很短", content="也很短"),
            Article(index=3, number="三", number_int=3, text="仍然很短", content="仍然很短"),
        ],
    )
    chunker = LawChunker(ChunkConfig())
    metas = [{"article_index": str(i), "chapter": "", "section": "",
              "part": "", "law_name": "测试法"} for i in range(1, 4)]
    chunks = chunker._chunk_articles(metas, doc)
    assert len(chunks) == 3
    assert all(c.metadata["article_count"] == "1" for c in chunks)


def test_merged_chunks_keep_article_prefix():
    """回归：开启合并时，合并块每条条文保留"第X条"前缀以便追溯"""
    from src.chunking.chunker import LawChunker
    doc = LawDocument(
        file_path="test.txt",
        title="测试法",
        parts=[],
        chapters=[],
        articles=[
            Article(index=1, number="一", number_int=1, text="很短", content="很短"),
            Article(index=2, number="二", number_int=2, text="也很短", content="也很短"),
        ],
    )
    chunker = LawChunker(ChunkConfig(merge_short_articles=True, max_merge_articles=3))
    metas = [{"article_index": str(i), "chapter": "第一章", "section": "",
              "part": "", "law_name": "测试法"} for i in range(1, 3)]
    chunks = chunker._chunk_articles(metas, doc)
    assert len(chunks) == 1
    assert "第一条 很短" in chunks[0].page_content
    assert "第二条 也很短" in chunks[0].page_content
    assert chunks[0].metadata["article_count"] == "2"


# ============================================================
# RetrievedDoc 数据模型
# ============================================================

def test_retrieved_doc_citation_full():
    doc = RetrievedDoc(
        content="测试内容",
        score=0.85,
        law_name="中华人民共和国治安管理处罚法(2025修订)",
        article_range="第十条",
    )
    assert "治安管理处罚法" in doc.citation
    assert "第十条" in doc.citation


def test_retrieved_doc_citation_no_article():
    doc = RetrievedDoc(
        content="测试", score=0.5,
        law_name="中华人民共和国民法典",
        chapter="第一章",
    )
    assert "民法典" in doc.citation
    assert "第一章" in doc.citation


def test_retrieved_doc_citation_empty():
    doc = RetrievedDoc(content="", score=0.0)
    assert doc.citation == ""


# ============================================================
# doc_to_retrieved — 文档对象 → RetrievedDoc 转换
# ============================================================

def test_doc_to_retrieved_basic():
    lc_doc = Document(
        page_content="条文正文内容",
        metadata={
            "law_name": "测试法",
            "chapter": "第一章",
            "section": "第一节",
            "article_range": "第一条至第三条",
            "chunk_type": "article",
        },
    )
    rd = doc_to_retrieved(lc_doc, 0.95)
    assert rd.content == "条文正文内容"
    assert rd.score == 0.95
    assert rd.law_name == "测试法"
    assert rd.chapter == "第一章"
    assert rd.section == "第一节"
    assert rd.article_range == "第一条至第三条"
    assert rd.chunk_type == "article"


def test_doc_to_retrieved_missing_metadata():
    lc_doc = Document(page_content="正文", metadata={})
    rd = doc_to_retrieved(lc_doc, 0.5)
    assert rd.content == "正文"
    assert rd.score == 0.5
    assert rd.law_name == ""
    assert rd.chapter == ""


def test_doc_to_retrieved_score_rounding():
    lc_doc = Document(page_content="x", metadata={})
    rd = doc_to_retrieved(lc_doc, 0.1234567)
    assert rd.score == 0.1235  # round to 4 decimal places
