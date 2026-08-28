"""
非流式 /api/chat 的 sources 归一化测试（M2 / F10）。

背景：非流式路径此前直接用 `retrieved_docs`（原始检索文档，无 verification），
与流式路径的 `fused_sources`（去重 + 来源加权 + 验证状态）行为不一致。
修复后非流式优先用融合结果，`from_rag_answer` 需同时兼容两种输入形态。
"""
from __future__ import annotations

from src.api.models import ChatResponse
from src.rag.retriever import RetrievedDoc


def _fused_source(**overrides):
    """融合后的来源条目（dict 形态，带 source / verification）。"""
    base = {
        "law_name": "中华人民共和国个人信息保护法",
        "chapter": "",
        "section": "",
        "article_range": "第三十六条",
        "citation": "中华人民共和国个人信息保护法第三十六条",
        "content": "国家机关处理的个人信息应当在中华人民共和国境内存储。",
        "score": 0.98,
        "source": "internal_kb",
        "verification": "verified_internal",
        "fused_score": 0.99,
    }
    base.update(overrides)
    return base


class TestFusedDictSources:
    def test_verification_and_source_preserved(self):
        """融合 dict 输入 → verification / source 字段透传给前端。"""
        resp = ChatResponse.from_rag_answer(
            query="q", answer="a",
            sources=[_fused_source()],
        )
        s = resp.sources[0]
        assert s["verification"] == "verified_internal"
        assert s["source"] == "internal_kb"
        assert s["law_name"] == "中华人民共和国个人信息保护法"
        assert s["score"] == 0.98

    def test_web_source_carries_url_and_unverified(self):
        """网络线索 → url + web_unverified，供前端徽章与警示条使用。"""
        resp = ChatResponse.from_rag_answer(
            query="q", answer="a",
            sources=[_fused_source(
                law_name="某解读文章",
                article_range="",
                citation="某解读文章",
                score=0.45,
                source="web",
                verification="web_unverified",
                url="https://example.com/a",
                superseded=True,
            )],
        )
        s = resp.sources[0]
        assert s["verification"] == "web_unverified"
        assert s["url"] == "https://example.com/a"
        assert s["superseded"] is True

    def test_legal_source_carries_law_status(self):
        """官方源 → verified_official + law_status（现行有效等）。"""
        resp = ChatResponse.from_rag_answer(
            query="q", answer="a",
            sources=[_fused_source(
                source="legal_source", verification="verified_official",
                law_status="现行有效", url="https://flk.npc.gov.cn/detail2.html?bbbs=x",
            )],
        )
        s = resp.sources[0]
        assert s["verification"] == "verified_official"
        assert s["law_status"] == "现行有效"


class TestRetrievedDocSources:
    def test_object_input_still_works(self):
        """RetrievedDoc 对象输入（固定管线回退）→ 正常，不带 verification。"""
        doc = RetrievedDoc(
            content="条文内容", score=0.9, law_name="《测试法》",
            chapter="第一章", section="", article_range="第一条",
        )
        resp = ChatResponse.from_rag_answer(query="q", answer="a", sources=[doc])
        s = resp.sources[0]
        assert s["law_name"] == "《测试法》"
        assert s["score"] == 0.9
        # 固定管线无融合 → 不含溯源字段
        assert "verification" not in s
        assert "source" not in s

    def test_mixed_input(self):
        """对象与 dict 混合（理论上不出现，但不应崩）。"""
        doc = RetrievedDoc(
            content="c", score=0.5, law_name="《A》",
            chapter="", section="", article_range="第1条",
        )
        resp = ChatResponse.from_rag_answer(
            query="q", answer="a", sources=[doc, _fused_source()],
        )
        assert len(resp.sources) == 2
        assert "verification" not in resp.sources[0]
        assert resp.sources[1]["verification"] == "verified_internal"


class TestDictsToRetrievedKeepsTraceFields:
    """回归：`_dicts_to_retrieved` 动态造对象时曾丢掉 source/verification/url。

    导致非流式 /api/chat 即使拿到融合结果，前端也看不到验证状态徽章。
    """

    def test_trace_fields_preserved(self):
        from src.api.routes import _dicts_to_retrieved

        objs = _dicts_to_retrieved([_fused_source(
            source="web", verification="web_unverified",
            url="https://example.com/a", superseded=True,
        )])
        obj = objs[0]
        assert obj.verification == "web_unverified"
        assert obj.source == "web"
        assert obj.url == "https://example.com/a"
        assert obj.superseded is True

    def test_plain_doc_has_no_trace_fields(self):
        """固定管线检索结果无溯源字段 → 不新增属性（getattr 走默认）。"""
        from src.api.routes import _dicts_to_retrieved

        obj = _dicts_to_retrieved([{"law_name": "《测试法》", "score": 0.9}])[0]
        assert obj.law_name == "《测试法》"
        assert not hasattr(obj, "verification")

    def test_end_to_end_verification_reaches_response(self):
        """端到端：融合 dict → _dicts_to_retrieved → from_rag_answer 仍带 verification。"""
        from src.api.routes import _dicts_to_retrieved

        objs = _dicts_to_retrieved([_fused_source(verification="verified_official")])
        resp = ChatResponse.from_rag_answer(query="q", answer="a", sources=objs)
        assert resp.sources[0]["verification"] == "verified_official"


class TestEdgeCases:
    def test_empty_sources(self):
        assert ChatResponse.from_rag_answer(query="q", answer="a", sources=[]).sources == []

    def test_missing_score_defaults_to_zero(self):
        resp = ChatResponse.from_rag_answer(
            query="q", answer="a", sources=[{"law_name": "无分条目"}],
        )
        assert resp.sources[0]["score"] == 0.0

    def test_falsy_extra_fields_omitted(self):
        """空字符串 / None / False 的溯源字段不写入（避免前端判空困扰）。"""
        resp = ChatResponse.from_rag_answer(
            query="q", answer="a",
            sources=[_fused_source(source="", verification=None, url="")],
        )
        s = resp.sources[0]
        assert "source" not in s
        assert "verification" not in s
        assert "url" not in s
