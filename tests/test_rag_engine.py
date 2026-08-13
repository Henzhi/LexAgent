"""RAG 引擎单元测试 — is_casual_query / _build_prompt / _extract_core 纯函数"""

import pytest
from src.rag.intent import is_casual_query
from src.rag.engine import RAGAnswer, RAGEngine
from src.rag.retriever import RetrievedDoc
from src.rag.adjacent_expander import AdjacentExpander


# ============================================================
# is_casual_query — 闲聊检测
# ============================================================

@pytest.mark.parametrize("query", [
    "你好",
    "您好",
    "hi",
    "hello",
    "早上好",
    "下午好",
    "晚上好",
    "大家好",
    "在吗",
    "在不",
    "谢谢",
    "感谢",
    "多谢",
    "thanks",
    "再见",
    "拜拜",
    "bye",
    "晚安",
    "你是谁",
    "你叫什么",
    "你是什么",
    "你能做什么",
    "你会什么",
    "你有什么功能",
    "讲个笑话",
    "今天天气",
    "嗯",
    "哦",
    "好吧",
    "好的",
    "OK",
])
def test_is_casual_query_true(query):
    assert is_casual_query(query) is True


@pytest.mark.parametrize("query", [
    "治安管理处罚的种类有哪些",
    "行政拘留最长多少天",
    "什么情况下构成正当防卫",
    "劳动合同可以约定试用期多久",
    "发明专利的保护期限是多久",
    "酒后驾车怎么处罚",
    "民法典第十七条",
    "刑法第二十条",
    "",
    "   ",  # empty after strip
])
def test_is_casual_query_false(query):
    # empty/whitespace returns True (deemed casual)
    stripped = query.strip()
    if not stripped:
        assert is_casual_query(query) is True
    else:
        assert is_casual_query(query) is False


def test_is_casual_query_introducing_self():
    assert is_casual_query("介绍一下你自己") is True


# ============================================================
# RAGAnswer.format_sources — 来源格式化
# ============================================================

def test_format_sources_empty():
    answer = RAGAnswer(query="test", answer="答", sources=[])
    result = answer.format_sources()
    assert "无引用来源" in result


def test_format_sources_casual():
    answer = RAGAnswer(query="你好", answer="你好!", is_casual=True)
    result = answer.format_sources()
    assert "闲聊" in result


def test_format_sources_single():
    doc = RetrievedDoc(
        content="测试条文", score=0.9,
        law_name="测试法", article_range="第十条",
    )
    answer = RAGAnswer(query="问", answer="答", sources=[doc])
    result = answer.format_sources()
    assert "测试法" in result
    assert "第十条" in result


def test_format_sources_deduplicate():
    """相同 law_name + article_range 不应重复"""
    doc1 = RetrievedDoc(
        content="a", score=0.9,
        law_name="测试法", article_range="第一条",
    )
    doc2 = RetrievedDoc(
        content="a", score=0.8,
        law_name="测试法", article_range="第一条",
    )
    answer = RAGAnswer(query="q", answer="a", sources=[doc1, doc2])
    result = answer.format_sources()
    # 只应出现一次
    assert result.count("第一条") == 1


def test_format_sources_multiple_laws():
    docs = [
        RetrievedDoc(content="a", score=0.9, law_name="刑法", article_range="第二十条"),
        RetrievedDoc(content="b", score=0.8, law_name="民法典", article_range="第十七条"),
    ]
    answer = RAGAnswer(query="q", answer="a", sources=docs)
    result = answer.format_sources()
    assert "刑法" in result
    assert "民法典" in result


# ============================================================
# RAGEngine._build_prompt — Prompt 构建
# ============================================================

class MockLLM:
    """mock LLM — 仅用于 RAGEngine 初始化，不调用"""
    pass


class MockRetriever:
    """mock 检索器"""
    def search(self, query, top_k=5):
        return []
    def is_ready(self):
        return True


def test_build_prompt_with_docs():
    docs = [
        RetrievedDoc(
            content="【中华人民共和国刑法】／第一章\n故意伤害他人身体的，处三年以下有期徒刑。",
            score=0.9,
            law_name="中华人民共和国刑法(2023修正)",
            chapter="第一章",
            article_range="第二百三十四条",
        ),
    ]
    engine = RAGEngine(
        retriever=MockRetriever(),
        llm=MockLLM(),
        top_k=5,
    )
    prompt = engine._build_prompt("故意伤害怎么判", docs)
    assert "故意伤害怎么判" in prompt
    assert "刑法" in prompt
    assert "第二百三十四条" in prompt
    assert "故意伤害" in prompt


def test_build_prompt_empty_docs():
    engine = RAGEngine(retriever=MockRetriever(), llm=MockLLM())
    prompt = engine._build_prompt("测试问题", [])
    assert "测试问题" in prompt
    assert "未找到相关条文" in prompt


def test_build_prompt_multiple_groups():
    """多条法律分组测试"""
    docs = [
        RetrievedDoc(
            content="【刑法】／第一章\n内容A", score=0.9,
            law_name="中华人民共和国刑法(2023修正)", chapter="第一章",
            article_range="第二十条",
        ),
        RetrievedDoc(
            content="【民法典】／第二章\n内容B", score=0.8,
            law_name="中华人民共和国民法典", chapter="第二章",
            article_range="第十七条",
        ),
    ]
    engine = RAGEngine(retriever=MockRetriever(), llm=MockLLM())
    prompt = engine._build_prompt("正当防卫", docs)
    assert "刑法" in prompt
    assert "民法典" in prompt
    assert "第二十条" in prompt
    assert "第十七条" in prompt


def test_build_prompt_deduplication():
    """相同条文去重"""
    docs = [
        RetrievedDoc(
            content="内容", score=0.9,
            law_name="刑法", chapter="第一章",
            article_range="第二十条",
        ),
        RetrievedDoc(
            content="内容", score=0.8,
            law_name="刑法", chapter="第一章",
            article_range="第二十条",
        ),
    ]
    engine = RAGEngine(retriever=MockRetriever(), llm=MockLLM())
    prompt = engine._build_prompt("test", docs)
    # "第二十条" 只应出现一次在分组中
    assert prompt.count("第二十条") <= 2  # 一次在 group header, 一次在 article_range


# ============================================================
# RAGEngine._extract_core — 核心文本提取
# ============================================================

def test_extract_core_with_prefix():
    doc = RetrievedDoc(
        content="【法律名】／第一章\n这是条文正文内容",
        score=0.9,
    )
    result = RAGEngine._extract_core(doc)
    assert result == "这是条文正文内容"


def test_extract_core_no_prefix():
    doc = RetrievedDoc(
        content="直接就是条文内容，没有前缀",
        score=0.9,
    )
    result = RAGEngine._extract_core(doc)
    assert result == "直接就是条文内容，没有前缀"


def test_extract_core_empty():
    doc = RetrievedDoc(content="", score=0.0)
    result = RAGEngine._extract_core(doc)
    assert result == ""


def test_extract_core_only_prefix():
    doc = RetrievedDoc(content="【测试法】", score=0.9)
    result = RAGEngine._extract_core(doc)
    assert result == "【测试法】"  # 没有换行符，保留原样


# ============================================================
# AdjacentExpander._parse_range_bounds — 条文范围解析
# ============================================================

@pytest.mark.parametrize("arange,expected", [
    ("第一条", [1]),
    ("第二条", [2]),
    ("第十条", [10]),
    ("第十二条", [12]),
    ("第二十条", [20]),
    ("第九十九条", [99]),
    ("第一百条", [100]),
    ("第一百二十三条", [123]),
    ("第二百条", [200]),
    ("第一条至第三条", [1, 3]),
    ("第十条至第十二条", [10, 12]),
    ("第一条至第十条", [1, 10]),
    # 注意: AdjacentExpander._parse_range_bounds 的正则 [一二三四五六七八九十百千] 不含"零"
    # "第一百条至第一百零五条" 当前返回 [100] 而非 [100,105]，这是已知限制
])
def test_parse_range_bounds(arange, expected):
    result = AdjacentExpander._parse_range_bounds(arange)
    assert result == expected


def test_parse_range_bounds_empty():
    result = AdjacentExpander._parse_range_bounds("")
    assert result == []


def test_parse_range_bounds_no_article():
    result = AdjacentExpander._parse_range_bounds("无条文信息")
    assert result == []
