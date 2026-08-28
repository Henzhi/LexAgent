"""
M2 双路融合测试（F6/F7/F8）：排序、去重、冲突裁决、验证状态标注。
"""
from __future__ import annotations

from src.search.fusion import (
    THIRD_PARTY,
    VERIFIED_INTERNAL,
    VERIFIED_OFFICIAL,
    WEB_UNVERIFIED,
    fuse_evidence,
)
from src.search.legal_sources import (
    SOURCE_COURT_CASE_LIB,
    SOURCE_NATIONAL_LAW_DB,
    SOURCE_XBG,
)


def _internal(law="《民事诉讼法》", article="第二百六十三条", score=0.9):
    return {
        "content": "执行程序相关条文内容",
        "score": score,
        "law_name": law,
        "chapter": "第三编",
        "section": "",
        "article_range": article,
        "chunk_type": "article",
        "citation": f"{law}{article}",
    }


def _web(title, url, score=0.9, content=""):
    return {"title": title, "url": url, "content": content or title, "score": score}


def _legal(title, url, sub=SOURCE_NATIONAL_LAW_DB, **extra):
    return {"title": title, "url": url, "content": "", "source": sub, **extra}


class TestOrderingAndDedup:
    def test_internal_ranks_first(self):
        """内部库权重最高，即使 web tavily score 满分也排后（F8 内部库优先）。"""
        fused = fuse_evidence(
            [_internal(score=0.3)],
            [_web("网络高分线索", "https://x.com/1", score=1.0)],
            [],
        )
        assert fused["sources"][0]["source"] == "internal_kb"
        assert fused["sources"][0]["verification"] == VERIFIED_INTERNAL
        assert fused["sources"][1]["verification"] == WEB_UNVERIFIED

    def test_internal_dedup_by_law_and_article(self):
        """内部库条目按 法名+条号 去重（F7）。"""
        fused = fuse_evidence(
            [_internal(), _internal(), _internal(article="第一百条")],
            [], [],
        )
        assert fused["count"] == 2

    def test_url_dedup_across_legal_and_web(self):
        """官方源与网络结果按 URL 去重，官方源先入（保留官方源）。"""
        fused = fuse_evidence(
            [],
            [_web("同一条目", "https://flk.npc.gov.cn/d1")],
            [_legal("民事诉讼法", "https://flk.npc.gov.cn/d1")],
        )
        assert fused["count"] == 1
        assert fused["sources"][0]["source"] == "legal_source"

    def test_web_dedup_by_url(self):
        fused = fuse_evidence(
            [],
            [_web("a", "https://x.com/1"), _web("b", "https://x.com/1"), _web("c", "https://x.com/2")],
            [],
        )
        assert fused["count"] == 2

    def test_top_k_truncation(self):
        docs = [_internal(article=f"第{i}条") for i in range(10)]
        fused = fuse_evidence(docs, [], [], top_k=5)
        assert fused["count"] == 5


class TestVerificationStatus:
    def test_legal_subsource_mapping(self):
        """官方源子来源 → 验证状态：法规库/案例库=verified_official，小包公=third_party。"""
        fused = fuse_evidence(
            [],
            [],
            [
                _legal("法", "https://flk/1", sub=SOURCE_NATIONAL_LAW_DB),
                _legal("案", "https://anli.court.gov.cn/1", sub=SOURCE_COURT_CASE_LIB),
                _legal("三方案", "https://xbg/1", sub=SOURCE_XBG),
            ],
        )
        by_url = {s["url"]: s["verification"] for s in fused["sources"]}
        assert by_url["https://flk/1"] == VERIFIED_OFFICIAL
        assert by_url["https://anli.court.gov.cn/1"] == VERIFIED_OFFICIAL
        assert by_url["https://xbg/1"] == THIRD_PARTY

    def test_web_always_unverified(self):
        fused = fuse_evidence([], [_web("线索", "https://x.com/1")], [])
        assert fused["sources"][0]["verification"] == WEB_UNVERIFIED


class TestConflictResolution:
    def test_web_mentioning_internal_law_marked_superseded(self):
        """web 结果提及内部库已收录法名 → superseded=True + conflict_laws 汇总（REQ-UW3）。"""
        fused = fuse_evidence(
            [_internal(law="《民事诉讼法》")],
            [_web("民事诉讼法最新修订解读", "https://x.com/1", content="《民事诉讼法》2026年有新变化")],
            [],
        )
        web_item = next(s for s in fused["sources"] if s["source"] == "web")
        assert web_item["superseded"] is True
        assert "民事诉讼法" in fused["conflict_laws"]
        assert fused["web_conflicts"] == 1

    def test_web_unrelated_no_conflict(self):
        """web 结果与内部库法名无重合 → 无冲突标记。"""
        fused = fuse_evidence(
            [_internal(law="《民事诉讼法》")],
            [_web("刑法修订新闻", "https://x.com/1", content="《刑法》相关")],
            [],
        )
        assert fused["web_conflicts"] == 0
        assert fused["conflict_laws"] == []
        web_item = next(s for s in fused["sources"] if s["source"] == "web")
        assert web_item["superseded"] is False

    def test_conflict_internal_still_first(self):
        """冲突时内部库条目仍排最前（内部库优先裁决）。"""
        fused = fuse_evidence(
            [_internal()],
            [_web("民事诉讼法新解读", "https://x.com/1", score=1.0)],
            [],
        )
        assert fused["sources"][0]["source"] == "internal_kb"


class TestEmptyInputs:
    def test_all_empty(self):
        fused = fuse_evidence([], [], [])
        assert fused["count"] == 0
        assert fused["sources"] == []
        assert fused["conflict_laws"] == []

    def test_web_only(self):
        fused = fuse_evidence([], [_web("线索", "https://x.com/1")], [])
        assert fused["count"] == 1
        assert fused["sources"][0]["source"] == "web"
