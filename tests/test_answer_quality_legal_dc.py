"""B3 评测管线单测：judge JSON 解析 / 聚合 / 报告渲染 / 提示词截断（纯函数，不触网络与真实 LLM）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "evaluation" / "scripts" / "eval_answer_quality_legal_dc.py"
_spec = importlib.util.spec_from_file_location("eval_answer_quality_legal_dc", _SCRIPT)
aq = importlib.util.module_from_spec(_spec)
sys.modules["eval_answer_quality_legal_dc"] = aq
_spec.loader.exec_module(aq)


class TestParseJudgeJson:
    def test_plain_json(self):
        out = aq.parse_judge_json('{"accuracy": 4, "completeness": 3, "reliability": 5, "overall": "good"}')
        assert out["accuracy"] == 4

    def test_json_in_fence_with_noise(self):
        text = '评估如下：\n```json\n{"accuracy": 2, "completeness": 2, "reliability": 1, "comment": "引用了不存在的条文"}\n```\n以上。'
        assert aq.parse_judge_json(text)["reliability"] == 1

    def test_missing_key_returns_none(self):
        assert aq.parse_judge_json('{"accuracy": 4}') is None

    def test_garbage_returns_none(self):
        assert aq.parse_judge_json("无法评分") is None
        assert aq.parse_judge_json("") is None

    def test_clamp_score(self):
        assert aq.clamp_score(0) == 1
        assert aq.clamp_score(9) == 5
        assert aq.clamp_score("3") == 3
        assert aq.clamp_score(None) is None
        assert aq.clamp_score("abc") is None


class TestAggregate:
    @staticmethod
    def _rows() -> list[dict]:
        return [
            {"src_id": 1, "class": "逻辑推理型", "accuracy": 5, "completeness": 4, "reliability": 5},
            {"src_id": 2, "class": "逻辑推理型", "accuracy": 3, "completeness": 3, "reliability": 3},
            {"src_id": 3, "class": "概括归纳型", "accuracy": 4, "completeness": 4, "reliability": 4},
            {
                "src_id": 4,
                "class": "概括归纳型",
                "accuracy": None,
                "completeness": None,
                "reliability": None,
                "comment": "JSON 解析失败",
            },
        ]

    def test_means_and_failed_count(self):
        agg = aq.aggregate(self._rows())
        assert agg["n"] == 3
        assert agg["failed"] == 1
        assert agg["accuracy"] == 4.0  # (5+3+4)/3
        assert agg["total_mean"] == 11.67  # (5+4+5)+(3+3+3)+(4+4+4) = 35/3

    def test_by_class_breakdown(self):
        agg = aq.aggregate(self._rows())
        assert agg["by_class"]["逻辑推理型"]["n"] == 2
        assert agg["by_class"]["概括归纳型"]["n"] == 1
        # 逻辑推理型三维总分 (5+4+5)=14、(3+3+3)=9 → 均值 11.5
        assert agg["by_class"]["逻辑推理型"]["overall"] == 11.5

    def test_low_reliability(self):
        rows = [{"src_id": 1, "class": "其他", "accuracy": 4, "completeness": 4, "reliability": 2}]
        assert aq.aggregate(rows)["low_reliability"] == 1

    def test_empty(self):
        agg = aq.aggregate([])
        assert agg["n"] == 0
        assert agg["by_class"] == {}


class TestRenderReport:
    def test_contains_overall_and_class_table(self):
        rows = [
            {"src_id": 1, "class": "逻辑推理型", "accuracy": 5, "completeness": 4, "reliability": 5},
            {"src_id": 2, "class": "概括归纳型", "accuracy": 3, "completeness": 3, "reliability": 2},
        ]
        agg = aq.aggregate(rows)
        md = aq.render_report(agg, gen_rows=[], meta={"source": "legal-dc@abc", "seed": 42, "subset_n": 150})
        assert "准确性" in md and "题型分型" in md
        assert "逻辑推理型" in md and "legal-dc@abc" in md

    def test_empty_report_no_crash(self):
        md = aq.render_report(aq.aggregate([]), gen_rows=[], meta={})
        assert "无有效评判结果" in md


class TestBuildJudgePrompt:
    def test_truncation_and_fields(self):
        item = {
            "query": "q" * 600,
            "reference_answer": "r" * 2000,
            "supporting_documents": ["d" * 400 for _ in range(5)],
        }
        prompt = aq.build_judge_prompt(item, "a" * 2000)
        # query≤500 + 参考/回答各≤1200 + 证据≤800 + 模板文案
        assert len(prompt) < aq.JUDGE_TEXT_MAX * 2 + aq.EVIDENCE_MAX + 900
        assert "（无）" not in prompt

    def test_missing_reference_shows_placeholder(self):
        prompt = aq.build_judge_prompt({"query": "q", "reference_answer": "", "supporting_documents": []}, "答案")
        assert "（无）" in prompt


class TestResumeHelpers:
    def test_load_jsonl_and_dedupe_last(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text(
            '{"src_id": 2, "error": "超时"}\n\n{"src_id": 1, "answer": "a"}\n{"src_id": 2, "answer": "补跑成功"}\n',
            encoding="utf-8",
        )
        rows = aq.load_jsonl(p)
        assert len(rows) == 3
        # 同 src_id 取最后一条（error 重试成功的最新行），按 src_id 排序输出
        assert [(r["src_id"], r.get("answer", "")) for r in aq.dedupe_last(rows)] == [(1, "a"), (2, "补跑成功")]
        assert aq.load_jsonl(tmp_path / "missing.jsonl") == []

    def test_done_gen_ids_retries_error_rows(self):
        rows = [
            {"src_id": 1, "error": ""},
            {"src_id": 2, "error": "URLError: 断网"},
            {"src_id": 3, "error": "", "confirmation_required": True},
        ]
        assert aq.done_gen_ids(rows) == {1, 3}  # error 行不算完成 → 重跑自动补

    def test_done_judge_ids_retries_unscored(self):
        rows = [
            {"src_id": 1, "accuracy": 4},
            {"src_id": 2, "accuracy": None, "comment": "judge 调用失败"},
        ]
        assert aq.done_judge_ids(rows) == {1}

    def test_result_paths_tag_isolation(self):
        g0, j0 = aq.result_paths("")
        assert g0.name == "aq_gen.jsonl" and j0.name == "aq_judge.jsonl"
        g1, j1 = aq.result_paths("review")
        assert g1.name == "aq_gen_review.jsonl" and j1.name == "aq_judge_review.jsonl"
        assert g1 != g0 and j1 != j0


class TestRunJudgeWritesTaggedPath:
    def test_judge_writes_to_given_path_not_default(self, tmp_path: Path, monkeypatch):
        """回归（2026-09-05 实证）：路径参数化只改签名漏改函数体写入点，
        25 条 review 判分被追加进基线 aq_judge.jsonl——judge 必须写调用方指定路径。"""
        import json as _json

        class _FakeLLM:
            def chat(self, prompt, history=None, system_prompt=None):
                return '{"accuracy": 4, "completeness": 4, "reliability": 4, "comment": "ok"}'

        monkeypatch.setattr("src.llm.factory.create_llm_backend", lambda **k: _FakeLLM())
        gen_rows = [{"src_id": 7, "class": "其他", "answer": "答案", "error": "", "confirmation_required": False}]
        subset_by_id = {7: {"query": "q", "reference_answer": "r", "supporting_documents": []}}
        target = tmp_path / "aq_judge_review.jsonl"
        aq.run_judge(gen_rows, subset_by_id, limit=10**9, judge_path=target)
        assert target.exists() and aq.done_judge_ids(aq.load_jsonl(target)) == {7}
        # 默认文件不被触碰
        assert (
            not (aq.JUDGE_PATH).exists()
            or aq.JUDGE_PATH.stat().st_size == 0
            or _json.loads(aq.JUDGE_PATH.read_text(encoding="utf-8").splitlines()[0]).get("src_id") != 7
        )
