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
            [],
            [],
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


class TestWebQuota:
    """网络线索保底配额：避免权威来源占满 top_k 后网络线索一条都不展示。"""

    def test_web_survives_when_authority_fills_top_k(self):
        """回归：权威条数 ≥ top_k 时，网络线索仍保底进入（否则 Tavily 白调用）。"""
        docs = [_internal(article=f"第{i}条") for i in range(10)]
        webs = [_web(f"线索{i}", f"https://x.com/{i}", score=0.9) for i in range(5)]
        fused = fuse_evidence(docs, webs, [], top_k=8)

        verifications = [s["verification"] for s in fused["sources"]]
        assert verifications.count(WEB_UNVERIFIED) == 2, "网络线索应保底占 2 个名额"
        assert fused["count"] == 8

    def test_authority_still_ranks_before_web(self):
        """配额不破坏优先级：权威来源仍排在网络线索之前。"""
        docs = [_internal(article=f"第{i}条", score=0.9) for i in range(4)]
        webs = [_web("线索", "https://x.com/1", score=1.0)]
        fused = fuse_evidence(docs, webs, [], top_k=8)

        srcs = fused["sources"]
        assert srcs[0]["verification"] == VERIFIED_INTERNAL
        assert srcs[-1]["verification"] == WEB_UNVERIFIED

    def test_quota_capped_by_web_count(self):
        """网络线索不足配额时，全部保留且权威来源补齐剩余名额（不浪费位置）。"""
        docs = [_internal(article=f"第{i}条") for i in range(6)]
        webs = [_web("唯一线索", "https://x.com/1", score=0.9)]
        fused = fuse_evidence(docs, webs, [], top_k=8)

        # 可用条目共 7 条（6 内部 + 1 网络），不足 top_k → 全部保留，不额外填充
        assert fused["count"] == 7
        assert sum(1 for s in fused["sources"] if s["verification"] == WEB_UNVERIFIED) == 1
        assert sum(1 for s in fused["sources"] if s["verification"] == VERIFIED_INTERNAL) == 6

    def test_no_web_means_no_quota_waste(self):
        """无网络线索时结果不受影响（仍按分截断到 top_k）。"""
        docs = [_internal(article=f"第{i}条") for i in range(10)]
        fused = fuse_evidence(docs, [], [], top_k=5)
        assert fused["count"] == 5
        assert all(s["verification"] == VERIFIED_INTERNAL for s in fused["sources"])

    def test_quota_zero_restores_pure_score_ordering(self):
        """配额设 0 → 退化为纯按分截断（可通过 FUSION_WEB_MIN_SLOTS=0 关闭）。"""
        docs = [_internal(article=f"第{i}条") for i in range(10)]
        webs = [_web(f"线索{i}", f"https://x.com/{i}", score=0.9) for i in range(5)]
        fused = fuse_evidence(docs, webs, [], top_k=8, web_min_slots=0)

        assert fused["count"] == 8
        assert all(s["verification"] == VERIFIED_INTERNAL for s in fused["sources"])

    def test_legal_still_prioritized_over_web(self):
        """官方源（0.85）优先于网络线索，配额只保证网络不被清零。"""
        legals = [_legal(f"法规{i}", f"https://flk/{i}") for i in range(10)]
        webs = [_web(f"线索{i}", f"https://x.com/{i}", score=1.0) for i in range(5)]
        fused = fuse_evidence([], webs, legals, top_k=8)

        veris = [s["verification"] for s in fused["sources"]]
        assert veris.count(VERIFIED_OFFICIAL) == 6
        assert veris.count(WEB_UNVERIFIED) == 2
        assert veris[0] == VERIFIED_OFFICIAL
