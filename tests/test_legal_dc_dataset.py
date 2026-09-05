"""B3 数据准备脚本单测：法名归一化 / 重叠判定 / 分层抽样（纯函数，不触网络）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "evaluation" / "scripts" / "build_legal_dc_dataset.py"
_spec = importlib.util.spec_from_file_location("build_legal_dc_dataset", _SCRIPT)
build_legal_dc_dataset = importlib.util.module_from_spec(_spec)
sys.modules["build_legal_dc_dataset"] = build_legal_dc_dataset
_spec.loader.exec_module(build_legal_dc_dataset)


class TestNormalizeTitle:
    def test_strips_book_marks_and_version(self):
        assert (
            build_legal_dc_dataset.normalize_title("《肉制品生产许可审查细则（2023版）》") == "肉制品生产许可审查细则"
        )

    def test_strips_halfwidth_revision(self):
        assert build_legal_dc_dataset.normalize_title("中华人民共和国刑法(2023修正)") == "中华人民共和国刑法"

    def test_year_revision_with_nian(self):
        assert (
            build_legal_dc_dataset.normalize_title("中华人民共和国安全生产法（2021年修订）")
            == "中华人民共和国安全生产法"
        )

    def test_plain_title_unchanged(self):
        assert build_legal_dc_dataset.normalize_title("专利代理管理办法") == "专利代理管理办法"

    def test_empty_and_whitespace(self):
        assert build_legal_dc_dataset.normalize_title("  《 X 法 》 ") == "X法"
        assert build_legal_dc_dataset.normalize_title("") == ""


class TestNormalizeClass:
    def test_strips_leading_space(self):
        assert build_legal_dc_dataset.normalize_class(" 逻辑推理型") == "逻辑推理型"

    def test_unknown_falls_to_other(self):
        assert build_legal_dc_dataset.normalize_class("脑洞型") == "其他"
        assert build_legal_dc_dataset.normalize_class("") == "其他"


class TestBuildOverlap:
    def test_overlap_matches_normalized(self):
        rows = [
            {"law_title": "肉制品生产许可审查细则（2023版）", "class": "概括归纳型"},
            {"law_title": "中华人民共和国刑法(2023修正)", "class": "其他"},
            {"law_title": "不存在的规章", "class": "其他"},
        ]
        kb = {"肉制品生产许可审查细则", "中华人民共和国刑法"}
        picked = build_legal_dc_dataset.build_overlap(rows, kb)
        assert len(picked) == 2

    def test_short_name_match(self):
        rows = [{"law_title": "刑法", "class": "其他"}]
        kb = {"中华人民共和国刑法", "刑法"}
        assert len(build_legal_dc_dataset.build_overlap(rows, kb)) == 1


class TestStratifiedSample:
    @staticmethod
    def _items(counts: dict[str, int]) -> list[dict]:
        items = []
        for cls, n in counts.items():
            items.extend({"class": cls, "i": i} for i in range(n))
        return items

    def test_exact_size_and_proportion(self):
        items = self._items({"概括归纳型": 100, "逻辑推理型": 50, "概念解释型": 30})
        picked = build_legal_dc_dataset.stratified_sample(items, 90, seed=1)
        assert len(picked) == 90
        counter = {c: sum(1 for p in picked if p["class"] == c) for c in ("概括归纳型", "逻辑推理型", "概念解释型")}
        assert counter == {"概括归纳型": 50, "逻辑推理型": 25, "概念解释型": 15}

    def test_no_duplicate(self):
        items = self._items({"概括归纳型": 10, "逻辑推理型": 5})
        picked = build_legal_dc_dataset.stratified_sample(items, 12, seed=3)
        keys = [(p["class"], p["i"]) for p in picked]
        assert len(keys) == len(set(keys)) == 12

    def test_seed_reproducible(self):
        items = self._items({"概括归纳型": 40, "逻辑推理型": 20, "概念解释型": 10})
        a = build_legal_dc_dataset.stratified_sample(items, 30, seed=42)
        b = build_legal_dc_dataset.stratified_sample(items, 30, seed=42)
        assert a == b

    def test_n_larger_than_pool_returns_all(self):
        items = self._items({"概括归纳型": 5})
        assert len(build_legal_dc_dataset.stratified_sample(items, 10)) == 5


class TestLoadProLawqa:
    def test_filters_blank_and_truncates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(build_legal_dc_dataset, "MAX_QUERY_LEN", 10)
        src = tmp_path / "pro_lawqa.json"
        src.write_text(
            json.dumps(
                [
                    {"query": "q", "answer": "a", "document": ["d1", " "], "title": "t", "class": " 逻辑推理型"},
                    {"query": "", "answer": "a", "title": "t", "class": "其他"},
                    {"query": "x" * 30, "answer": "a", "document": [], "title": "t", "class": "其他"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rows = build_legal_dc_dataset.load_pro_lawqa(src)
        assert len(rows) == 2
        assert rows[0]["class"] == "逻辑推理型"
        assert rows[0]["supporting_documents"] == ["d1"]
        assert len(rows[1]["query"]) == 10
