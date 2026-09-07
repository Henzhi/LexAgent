"""版本冲突判定脚本的单测（DB-free，只测纯函数与 classify_group）。

覆盖三种真实形态：
  1. 真版本冲突——新版覆盖旧版全部条文且版本更旧 → superseded（刑法类）；
  2. 合法并存——修正案系列 → keep，不得误判（宪法修正案类）；
  3. 重复入库——去空白内容一致 → duplicate（文件名尾部空格类）。
另覆盖：无法判定 → ambiguous，不自动处置。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.scripts.check_law_version_conflicts import (  # noqa: E402
    LawDoc,
    base_law_name,
    classify_group,
    compare_articles,
    content_fingerprint,
    coverage,
    extract_article_numbers,
    jaccard,
    max_document_date,
    strip_offense_labels,
    title_year,
    version_score,
)


def _doc(doc_id: str, title: str, chunks: list[str]) -> LawDoc:
    return LawDoc(doc_id, title, chunks)


class TestTitleNormalization:
    def test_strip_file_ext_and_version_suffix(self):
        assert base_law_name("中华人民共和国刑法(2023修正)") == "中华人民共和国刑法"
        assert base_law_name("中华人民共和国刑法.txt") == "中华人民共和国刑法"

    def test_trailing_space_in_filename_collapses(self):
        # 文件名尾部空格是重复入库的根因，归一化后必须落到同一 base
        assert base_law_name("公司法(2023修订) ") == "公司法"
        assert base_law_name("公司法(2023修订)") == "公司法"

    def test_fullwidth_and_halfwidth_bracket(self):
        assert base_law_name("宪法（2018年修正文本）") == "宪法"

    def test_title_year(self):
        assert title_year("中华人民共和国刑法(2023修正)") == 2023
        assert title_year("中华人民共和国刑法") is None


class TestDateExtraction:
    def test_max_full_date(self):
        text = "（2005年8月28日通过　根据2012年10月26日《关于修改…》修正）"
        score, raw = max_document_date(text)
        assert raw == "2012年10月26日"
        assert score == 20121026

    def test_fallback_year_only(self):
        score, raw = max_document_date("根据 1999年 的决定修订")
        assert score == 19990000
        assert raw == "1999年"

    def test_no_date(self):
        assert max_document_date("第一条　为了…") == (None, "")


class TestArticleExtraction:
    def test_extract_plain_and_sub(self):
        text = "第一条　为了…。第二条　本法所称…。第一百六十五条之一　其他公司…"
        nums = extract_article_numbers(text)
        assert {"第一条", "第二条", "第一百六十五条之一"} <= nums

    def test_arabic_and_chinese_numerals_unify(self):
        # 不同批次 ingest 可能出现「第165条」与「第一百六十五条」混用，
        # 不统一会把同一条文算成两条，系统性低估覆盖率。
        assert extract_article_numbers("第165条 内容") == extract_article_numbers("第一百六十五条 内容")

    def test_multiple_digits_conversion(self):
        assert extract_article_numbers("第20条") == {"第二十条"}
        assert extract_article_numbers("第1000条") == {"第一千条"}

    def test_coverage_and_jaccard(self):
        old = {"第一条", "第二条", "第三条"}
        new = {"第一条", "第二条", "第三条", "第四条"}
        assert coverage(new, old) == 1.0
        assert coverage(old, new) == 3 / 4
        assert jaccard(old, new) == 0.75


class TestFingerprint:
    def test_whitespace_insensitive(self):
        assert content_fingerprint("第一条 内容\n\n") == content_fingerprint("第一条内容")

    def test_different_content_differs(self):
        assert content_fingerprint("第一条") != content_fingerprint("第二条")


class TestVersionScore:
    def test_title_year_wins_over_doc_date(self):
        d = _doc("1", "刑法(2023修正)", ["1979年7月1日通过"])
        score, source = version_score(d)
        assert source == "title=2023"
        assert score // 10000 == 2023

    def test_fallback_to_doc_date(self):
        d = _doc("1", "治安管理处罚法", ["2005年8月28日通过　根据2012年10月26日修正"])
        score, source = version_score(d)
        assert source.startswith("doc_date=")
        assert score == 20121026

    def test_unknown_version(self):
        assert version_score(_doc("1", "某法", ["第一条"])) == (0, "unknown")


def _law_text(prefix_articles: int, extra: str = "") -> list[str]:
    return [f"第{i}条　内容{i}" for i in range(1, prefix_articles + 1)] + ([extra] if extra else [])


class TestArticleBodyCompare:
    """判决度的核心：duplicate 与 superseded 必须靠**条文正文**区分，不能靠编号/长度。"""

    def test_offense_label_is_noise(self):
        old = {"第一百六十五条": "国有公司、企业的董事利用职务便利…"}
        new = {"第一百六十五条": "第一百六十五条【非法经营同类营业罪】国有公司、企业的董事利用职务便利…"}
        # 剥掉【罪名】标题后正文一致 → 零差异（不同爬虫来源的排版差异）
        stripped = {k: strip_offense_labels(v) for k, v in new.items()}
        diff, common = compare_articles(stripped, old)
        assert common == 1
        assert diff == 1  # 未剥离时确实不同，故剥离是必需步骤

    def test_real_substantive_diff_is_counted(self):
        old = {"第一百六十五条": "国有公司、企业的董事、经理利用职务便利…"}
        new = {"第一百六十五条": "国有公司、企业的董事、监事、高级管理人员利用职务便利…"}
        diff, common = compare_articles(new, old)
        assert (diff, common) == (1, 1)

    def test_identical_body_zero_diff(self):
        diff, common = compare_articles(
            {"第一条": "内容甲", "第二条": "内容乙"}, {"第一条": "内容甲", "第二条": "内容乙"}
        )
        assert (diff, common) == (0, 2)


class TestClassifyGroup:
    def test_true_version_conflict_marks_old_superseded(self):
        # 旧版第 3 条是旧表述，新版已修订 → 真版本冲突
        old = _doc("old", "某法", ["1979年1月1日通过", "第一条内容", "第二条内容", "第三条旧表述"])
        new = _doc("new", "某法(2023修正)", ["根据2023年1月1日修正", "第一条内容", "第二条内容", "第三条新表述"])
        rows = classify_group("某法", [old, new])
        verdict = {r["title"]: r["role"] for r in rows}
        assert verdict["某法(2023修正)"] == "current"
        assert verdict["某法"] == "superseded"
        sup = next(r for r in rows if r["role"] == "superseded")
        assert "1 条实质差异" in sup["evidence"]

    def test_same_article_numbers_and_length_but_real_diff_is_not_duplicate(self):
        """回归守卫：编号与长度都接近，但有实质差异时必须判 version conflict。

        真实案例——刑法两版同为 452 条、长度接近，仅 7 条实质差异（修正案十二），
        早期「长度 + 条文号 Jaccard」判据会把它误判成重复入库。
        """
        arts = [f"第{i}条　内容{i}" for i in range(1, 11)]
        old = _doc("old", "某法", ["1979年1月1日通过"] + arts)
        new_arts = list(arts)
        new_arts[4] = "第五条　修订后的新表述"
        new = _doc("new", "某法(2023修正)", ["根据2023年1月1日修正"] + new_arts)
        rows = classify_group("某法", [old, new])
        roles = {r["title"]: r["role"] for r in rows}
        assert roles["某法"] == "superseded"
        assert "duplicate" not in roles.values()

    def test_duplicate_same_content_different_chunking(self):
        # 同一份文件的两种切分：去空白内容一致 → duplicate
        a = _doc("a", "某法(2023修订)", ["第一条内容一第二条内容二"])
        b = _doc("b", "某法(2023修订) ", ["第一条内容一", "第二条内容二"])
        rows = classify_group("某法", [a, b])
        verdict = {r["title"]: r["role"] for r in rows}
        assert sum(v == "duplicate" for v in verdict.values()) == 1
        assert "current" in verdict.values()

    def test_amendment_series_is_kept(self):
        docs = [
            _doc(f"a{i}", f"宪法修正案（{y}年）", [f"第{i}条　{y}年修正"])
            for i, y in enumerate([1988, 1993, 1999], start=1)
        ]
        rows = classify_group("宪法修正案", docs)
        assert {r["role"] for r in rows} == {"keep"}

    def test_low_coverage_is_ambiguous_not_superseded(self):
        old = _doc("old", "某法", ["1979年1月1日通过"] + _law_text(10))
        new = _doc("new", "某法(2023修正)", _law_text(2))
        rows = classify_group("某法", [old, new])
        verdict = {r["title"]: r["role"] for r in rows}
        assert verdict["某法"] == "ambiguous"

    def test_rows_carry_evidence_and_more_chunks_win_ties(self):
        thin = _doc("thin", "某法(2023修正)", ["第一条"])
        rich = _doc("rich", "某法(2023修正)", ["第一条", "第二条", "第三条"])
        rows = classify_group("某法", [thin, rich])
        current = [r for r in rows if r["role"] == "current"]
        assert len(current) == 1
        assert current[0]["doc_id"] == "rich"
        assert current[0]["evidence"]
