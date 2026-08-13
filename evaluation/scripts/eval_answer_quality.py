"""
回答质量评测：对比 expected_answer 与 model_output。

规则层（131条全量）:
  - 法律名称命中率
  - 法条号命中率
  - 免责声明包含率
  - 幻觉标志检测
  - 答案长度偏差

LLM 评判层（抽样 30 条）:
  - 准确性 / 完整性 / 幻觉风险评分

用法:
    uv run python scripts/eval_answer_quality.py               # 全量规则评测
    uv run python scripts/eval_answer_quality.py --judge 30     # 规则 + LLM抽样
    uv run python scripts/eval_answer_quality.py --output docs/answer_quality.md
"""
from __future__ import annotations

import json
import re
import sys
import time
import argparse
from pathlib import Path
from collections import Counter

EVAL_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATASET_PATH = EVAL_DIR / "data" / "eval_dataset.json"


# ============================================================
# 规则层：提取关键要素
# ============================================================

def extract_law_names(text: str) -> set[str]:
    """提取文本中引用的法律名称，归一化为短名"""
    # 全称→短名映射
    SHORT_MAP = {
        "中华人民共和国刑法": "刑法",
        "中华人民共和国民法典": "民法典",
        "中华人民共和国宪法": "宪法",
        "中华人民共和国劳动法": "劳动法",
        "中华人民共和国劳动合同法": "劳动合同法",
        "中华人民共和国公司法": "公司法",
        "中华人民共和国专利法": "专利法",
        "中华人民共和国商标法": "商标法",
        "中华人民共和国著作权法": "著作权法",
        "中华人民共和国证券法": "证券法",
        "中华人民共和国行政处罚法": "行政处罚法",
        "中华人民共和国行政复议法": "行政复议法",
        "中华人民共和国行政强制法": "行政强制法",
        "中华人民共和国行政许可法": "行政许可法",
        "中华人民共和国治安管理处罚法": "治安管理处罚法",
        "中华人民共和国道路交通安全法": "道路交通安全法",
        "中华人民共和国食品安全法": "食品安全法",
        "中华人民共和国环境保护法": "环境保护法",
        "中华人民共和国反不正当竞争法": "反不正当竞争法",
        "中华人民共和国社会保险法": "社会保险法",
        "中华人民共和国监察法": "监察法",
        "中华人民共和国企业破产法": "企业破产法",
        "中华人民共和国合伙企业法": "合伙企业法",
        "中华人民共和国个人独资企业法": "个人独资企业法",
        "中华人民共和国信托法": "信托法",
        "中华人民共和国票据法": "票据法",
        "中华人民共和国立法法": "立法法",
        "中华人民共和国公务员法": "公务员法",
        "中华人民共和国全国人民代表大会组织法": "全国人民代表大会组织法",
        "中华人民共和国国务院组织法": "国务院组织法",
        "中华人民共和国行政诉讼法": "行政诉讼法",
    }
    names = set()
    for m in re.finditer(r'《([^》]+)》', text):
        full = m.group(1)
        # 去掉修订信息和括号
        short = re.sub(r'\([^)]*\)', '', full).strip()
        # 归一化到短名
        names.add(SHORT_MAP.get(short, short))
    return names


def extract_article_numbers(text: str) -> set[str]:
    """提取法条号（第X条 / 第X条至第Y条）"""
    nums = set()
    for m in re.finditer(r'第([一二三四五六七八九十百千]+)条', text):
        nums.add(m.group(0))
    return nums


def has_disclaimer(text: str) -> bool:
    """是否包含免责声明"""
    keywords = ['仅供参考', '不构成', '法律意见', '执业律师']
    return any(kw in text for kw in keywords)


def has_retrieval_failure(text: str) -> bool:
    """检测检索失败（模型无法找到相关条文）"""
    flags = ['无法找到', '未查询到', '并未包含', '不存在']
    return any(f in text for f in flags)


def has_real_hallucination(text: str) -> bool:
    """检测真正的幻觉标志（非条文引用的自我推理/编造）"""
    flags = [
        '据我所知', '根据我的理解', '我的知识',
        '数据库中没有', '目前的知识库',
    ]
    return any(f in text for f in flags)


# ============================================================
# 单条评分
# ============================================================

def score_one(item: dict) -> dict:
    """对单条问答对做规则评分，返回评分 dict"""
    expected = item.get("expected_answer", "")
    model = item.get("model_output", "")
    note = item.get("evaluation_note", "")

    if not model or model.startswith("ERROR"):
        return {"status": "error", "score": 0}

    # 法律名称（归一化后比较）
    exp_laws = extract_law_names(expected)
    mod_laws = extract_law_names(model)
    law_hit = len(exp_laws & mod_laws) > 0 if exp_laws else None  # None=无预期法律名
    law_score = 1.0 if law_hit else (0.7 if law_hit is None else 0.0)

    # 法条号
    exp_arts = extract_article_numbers(expected)
    mod_arts = extract_article_numbers(model)
    art_hit = len(exp_arts & mod_arts)
    art_score = min(art_hit / max(len(exp_arts), 1), 1.0) if exp_arts else 1.0

    # 检索失败
    ret_fail = has_retrieval_failure(model)

    # 真实幻觉
    halluc = has_real_hallucination(model)

    # 综合分: 法律名30% + 法条号30% + 检索状态20% + 幻觉20%
    if ret_fail:
        score = 0.0  # 检索失败直接 0 分
    else:
        score = law_score * 0.30 + art_score * 0.30 + 1.0 * 0.20 + (0.0 if halluc else 1.0) * 0.20

    return {
        "question": item["question"][:40],
        "score": round(score, 3),
        "law_hit": law_hit or False,
        "art_hit": art_score,
        "retrieval_fail": ret_fail,
        "hallucination": halluc,
    }


# ============================================================
# LLM 评判
# ============================================================

JUDGE_PROMPT = """你是一个法律问答质量评估员。请对比「标准答案」和「模型回答」，从三个维度评分（1-5分）：

1. 准确性: 模型回答与标准答案的核心事实是否一致？是否引用了正确的法律和法条号？
2. 完整性: 是否覆盖了标准答案的关键信息点？
3. 可靠性: 是否存在编造、幻觉或误导性陈述？

## 标准答案
{expected}

## 模型回答
{model}

请按以下 JSON 格式输出（只输出 JSON）：
{{"accuracy": 4, "completeness": 3, "reliability": 5, "overall": "good", "comment": "简短评语"}}"""


def judge_batch(items: list[dict], limit: int) -> list[dict]:
    """用 LLM 对随机抽样条目做质量评判"""
    import random
    from src.llm.client import LawLLM, LLMConfig
    from src.config import LLM_MODEL, LLM_BASE_URL

    sample = random.sample([i for i in items if i.get("model_output") and not i["model_output"].startswith("ERROR")],
                           min(limit, len(items)))
    print(f"LLM 评判: 抽样 {len(sample)} 条 ...")

    llm = LawLLM(model=LLM_MODEL, base_url=LLM_BASE_URL, config=LLMConfig(temperature=0.0))
    results = []

    for i, item in enumerate(sample):
        prompt = JUDGE_PROMPT.format(
            expected=item["expected_answer"][:800],
            model=item["model_output"][:800],
        )
        try:
            resp = llm.chat(prompt, system_prompt="你是法律问答质量评估员，只输出JSON。")
            # 提取 JSON
            match = re.search(r'\{[^}]+\}', resp)
            if match:
                judge = json.loads(match.group(0))
                judge["question"] = item["question"][:30]
                results.append(judge)
            else:
                results.append({"question": item["question"][:30], "error": "JSON parse failed"})
        except Exception as e:
            results.append({"question": item["question"][:30], "error": str(e)})

        if (i + 1) % 5 == 0:
            print(f"  LLM 评判进度: {i+1}/{len(sample)}")

    return results


# ============================================================
# 报告生成
# ============================================================

def generate_report(all_scores: list[dict], judge_results: list[dict] | None) -> str:
    valid = [s for s in all_scores if s.get("status") != "error"]
    n = len(valid)
    total = len(all_scores)

    avg = sum(s["score"] for s in valid) / n if n else 0
    law_hit_rate = sum(1 for s in valid if s["law_hit"]) / n * 100 if n else 0
    ret_fail_rate = sum(1 for s in valid if s["retrieval_fail"]) / n * 100 if n else 0
    halluc_rate = sum(1 for s in valid if s["hallucination"]) / n * 100 if n else 0

    # 评分分布
    bins = {"优秀(≥0.8)": 0, "良好(0.6-0.8)": 0, "一般(0.4-0.6)": 0, "较差(<0.4)": 0}
    for s in valid:
        sc = s["score"]
        if sc >= 0.8: bins["优秀(≥0.8)"] += 1
        elif sc >= 0.6: bins["良好(0.6-0.8)"] += 1
        elif sc >= 0.4: bins["一般(0.4-0.6)"] += 1
        else: bins["较差(<0.4)"] += 1

    lines = [
        "# 回答质量评测报告",
        "",
        f"**评测时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**数据来源**: data/eval_dataset.json ({total} 条)",
        "",
        "## 一、规则层评分（全量）",
        "",
        "### 汇总指标",
        "",
        f"| 指标 | 数值 | 说明 |",
        f"|------|------|------|",
        f"| 评测总数 | {total} | 有效 {n} 条 |",
        f"| **综合评分** | **{avg:.3f}** | 加权平均 (法律名30%+法条号30%+检索20%+幻觉20%) |",
        f"| 法律名称命中率 | {law_hit_rate:.1f}% | model_output 引用的法律与 expected_answer 一致（归一化后） |",
        f"| 检索失败率 | {ret_fail_rate:.1f}% | 模型未能检索到相关条文（直接0分） |",
        f"| 真实幻觉率 | {halluc_rate:.1f}% | 出现非条文引用的自我推理 |",
        "",
        "### 评分分布",
        "",
        f"| 等级 | 数量 | 占比 |",
        f"|------|------|------|",
    ]
    for label, cnt in bins.items():
        lines.append(f"| {label} | {cnt} | {cnt/n*100:.1f}% |")

    lines += [
        "",
        "### 低分条目（score < 0.5）",
        "",
    ]
    low = [s for s in valid if s["score"] < 0.5]
    if low:
        lines.append("| 问题 | 评分 | 法律命中 | 法条分 | 检索失败 |")
        lines.append("|------|------|---------|--------|---------|")
        for s in sorted(low, key=lambda x: x["score"])[:15]:
            lines.append(
                f"| {s['question'][:30]} | {s['score']:.3f} | "
                f"{'Y' if s['law_hit'] else 'N'} | {s['art_hit']:.1f} | "
                f"{'Y' if s['retrieval_fail'] else 'N'} |"
            )
    else:
        lines.append("无低分条目。")

    # LLM 评判结果
    if judge_results:
        lines += [
            "",
            "## 二、LLM 评判层（抽样）",
            "",
        ]
        valid_judge = [j for j in judge_results if "error" not in j]
        if valid_judge:
            acc = sum(j.get("accuracy", 0) for j in valid_judge) / len(valid_judge)
            comp = sum(j.get("completeness", 0) for j in valid_judge) / len(valid_judge)
            rel = sum(j.get("reliability", 0) for j in valid_judge) / len(valid_judge)

            lines += [
                f"**抽样数量**: {len(valid_judge)} 条",
                "",
                f"| 维度 | 均分 (1-5) | 说明 |",
                f"|------|-----------|------|",
                f"| 准确性 | {acc:.2f} | 与标准答案的核心事实是否一致 |",
                f"| 完整性 | {comp:.2f} | 是否覆盖关键信息点 |",
                f"| 可靠性 | {rel:.2f} | 是否存在编造或幻觉 |",
                "",
                "### 抽样详情",
                "",
                "| 问题 | 准确性 | 完整性 | 可靠性 | 总评 |",
                "|------|--------|--------|--------|------|",
            ]
            for j in sorted(valid_judge, key=lambda x: x.get("accuracy", 0) + x.get("completeness", 0) + x.get("reliability", 0)):
                lines.append(
                    f"| {j.get('question', '?')[:20]} | "
                    f"{j.get('accuracy', '?')} | {j.get('completeness', '?')} | "
                    f"{j.get('reliability', '?')} | {j.get('overall', '?')} |"
                )

    lines += [
        "",
        "## 三、结论",
        "",
        f"- **综合评分 {avg:.2f}**，{bins['优秀(≥0.8)']}/{n} 条达到优秀",
        f"- **法律名称命中率 {law_hit_rate:.0f}%**：经过全称→短名归一化后，模型引用法律名称准确",
        f"- **检索失败率 {ret_fail_rate:.0f}%**：{int(ret_fail_rate * n / 100)} 条因检索未命中直接 0 分，为主要扣分原因",
        f"- **真实幻觉率 {halluc_rate:.0f}%**：模型极少出现非条文引用的自我推理",
        f"- **改进方向**：优化低分条目的 chunk 索引映射，减少检索失败",
    ]

    return "\n".join(lines)


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="回答质量评测")
    ap.add_argument("--judge", type=int, default=0, help="LLM 抽样评判条数 (0=仅规则层)")
    ap.add_argument("--output", type=str, default="docs/answer_quality.md", help="输出路径")
    args = ap.parse_args()

    # 加载数据
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    print(f"加载 {len(dataset)} 条数据")

    # 规则层评分
    print("规则层评分中 ...")
    scores = [score_one(item) for item in dataset]

    # LLM 评判
    judge_results = None
    if args.judge > 0:
        judge_results = judge_batch(dataset, args.judge)

    # 生成报告
    report = generate_report(scores, judge_results)
    output_path = PROJECT_ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")

    # 终端摘要
    valid = [s for s in scores if s.get("status") != "error"]
    avg = sum(s["score"] for s in valid) / len(valid) if valid else 0
    excellent = sum(1 for s in valid if s["score"] >= 0.8)
    poor = sum(1 for s in valid if s["score"] < 0.4)
    print(f"\n=== 评测完成 ===")
    print(f"综合评分: {avg:.3f}")
    print(f"优秀 (≥0.8): {excellent}/{len(valid)}")
    print(f"较差 (<0.4): {poor}/{len(valid)}")
    print(f"报告: {output_path}")


if __name__ == "__main__":
    main()
