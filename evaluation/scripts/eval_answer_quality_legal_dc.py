"""B3 回答质量评测管线：Legal-DC 重叠子集 → 真实系统生成回答 → LLM-as-judge 评分 → 题型分型报告。

前置：先跑 build_legal_dc_dataset.py --sample 150 产出 answer_quality_subset.json。

三条子命令（各自独立、断点续跑，结果均为追加式 JSONL）：
  --generate N  对前 N 条（跳过已完成）调真实系统 /api/chat/stream 生成回答
  --judge N     对已有回答的条目（跳过已评判）用 LLM-as-judge 打分（准确性/完整性/可靠性 1-5）
  --report      汇总评判结果 → Markdown 报告（整体 + 题型分型 + 低分条目）

judge 用主 LLM 后端（DeepSeek，经 create_llm_backend 统一入口，带预算埋点）。
外部仓库无 LICENSE，结果文件全部落在 gitignore 的 evaluation/data/legal_dc/ 下。

用法:
  uv run python evaluation/scripts/eval_answer_quality_legal_dc.py --generate 150
  uv run python evaluation/scripts/eval_answer_quality_legal_dc.py --judge 0      # 0=全部已生成条目
  uv run python evaluation/scripts/eval_answer_quality_legal_dc.py --report --output docs/B3-答案质量基线报告.md
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = EVAL_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))  # 延迟导入 src.llm.factory 用（judge 走统一后端入口）
DATA_DIR = EVAL_DIR / "data" / "legal_dc"
SUBSET_PATH = DATA_DIR / "answer_quality_subset.json"
RESULTS_DIR = DATA_DIR / "results"
GEN_PATH = RESULTS_DIR / "aq_gen.jsonl"  # 默认 tag（基线）；对比跑用 --tag review 等隔离
JUDGE_PATH = RESULTS_DIR / "aq_judge.jsonl"


def result_paths(tag: str) -> tuple[Path, Path]:
    """按 tag 隔离结果文件（基线与带审核的对比跑不混写；paired 对比要同题两份答案）。"""
    suffix = f"_{tag}" if tag else ""
    return RESULTS_DIR / f"aq_gen{suffix}.jsonl", RESULTS_DIR / f"aq_judge{suffix}.jsonl"


# 复用 lexeval_eval 的 SSE 解析与后端约定（端口 8001）
_spec = importlib.util.spec_from_file_location("lexeval_eval", EVAL_DIR / "scripts" / "lexeval_eval.py")
lexeval_eval = importlib.util.module_from_spec(_spec)
sys.modules["lexeval_eval"] = lexeval_eval
_spec.loader.exec_module(lexeval_eval)

BASE_URL_DEFAULT = "http://127.0.0.1:8001"

JUDGE_SYSTEM = "你是法律问答质量评估员，只输出 JSON，不要输出任何其他内容。"

JUDGE_PROMPT = """请对比「参考答案」与「系统回答」，对下面的法律咨询问答从三个维度打分（1-5 整数）：
1. accuracy 准确性：系统回答与参考答案的核心事实/法律依据是否一致，有无错误陈述；
2. completeness 完整性：参考答案的关键信息点是否被覆盖；
3. reliability 可靠性：有无编造法条、幻觉引用或误导性表述。

## 用户问题
{query}

## 参考答案（标注数据）
{reference}

## 支撑段落（原始出处节选）
{evidence}

## 系统回答（被评对象）
{model_output}

只输出如下 JSON：{{"accuracy": 1, "completeness": 1, "reliability": 1, "overall": "good/bad", "comment": "一句话评语"}}"""

# 送审文本截断：单侧过长会让 judge 抓不住重点，且参考答案本身很短
JUDGE_TEXT_MAX = 1200
EVIDENCE_MAX = 800


# ============================================================
# 纯函数（单测覆盖）
# ============================================================


def parse_judge_json(text: str) -> dict | None:
    """从 LLM 输出提取评分 JSON；容忍 ```json 围栏与前后杂文，失败返回 None。"""
    if not text:
        return None
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not all(k in obj for k in ("accuracy", "completeness", "reliability")):
        return None
    return obj


def clamp_score(v, lo: int = 1, hi: int = 5) -> int | None:
    """评分钳制到 [1,5]；非整数返回 None（计数为解析失败）。"""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, iv))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def done_ids(rows: list[dict]) -> set[int]:
    """已完成的 src_id 集合（续跑跳过用）。"""
    return {r["src_id"] for r in rows if "src_id" in r}


def dedupe_last(rows: list[dict]) -> list[dict]:
    """同 src_id 多行时取**最后一条**（error 行重试成功后同 id 两行，消费端只认最新）。"""
    out: dict[int, dict] = {}
    for r in rows:
        if "src_id" in r:
            out[r["src_id"]] = r
    return [out[k] for k in sorted(out)]


def done_gen_ids(rows: list[dict]) -> set[int]:
    """生成层"已完成"判定：**error 行不算完成**——断网等故障的失败条目重跑时自动补跑。"""
    return {r["src_id"] for r in rows if "src_id" in r and not r.get("error")}


def done_judge_ids(rows: list[dict]) -> set[int]:
    """评判层"已完成"判定：打分缺失（解析/调用失败）不算完成，重跑自动补评。"""
    return {r["src_id"] for r in rows if r.get("accuracy") is not None}


def aggregate(judged: list[dict]) -> dict:
    """聚合评判结果：整体均值 + 题型分型 + 分数分布。

    判分解析失败的条目单独计数，不进均值（与生成失败口径一致：坏行可见但不污染指标）。
    """
    scored = [r for r in judged if r.get("accuracy") is not None]
    failed = len(judged) - len(scored)
    if not scored:
        return {"n": 0, "failed": failed, "by_class": {}}

    def mean(vals: list[int]) -> float:
        return round(statistics.fmean(vals), 2)

    by_class: dict[str, dict] = {}
    for cls in sorted({r["class"] for r in scored}):
        rows = [r for r in scored if r["class"] == cls]
        by_class[cls] = {
            "n": len(rows),
            "accuracy": mean([r["accuracy"] for r in rows]),
            "completeness": mean([r["completeness"] for r in rows]),
            "reliability": mean([r["reliability"] for r in rows]),
            "overall": mean([r["accuracy"] + r["completeness"] + r["reliability"] for r in rows]),
        }
    total_scores = [r["accuracy"] + r["completeness"] + r["reliability"] for r in scored]
    return {
        "n": len(scored),
        "failed": failed,
        "accuracy": mean([r["accuracy"] for r in scored]),
        "completeness": mean([r["completeness"] for r in scored]),
        "reliability": mean([r["reliability"] for r in scored]),
        "total_mean": round(statistics.fmean(total_scores), 2),
        "total_max": 15,
        "score_dist": dict(collections.Counter(total_scores)),
        "low_reliability": sum(1 for r in scored if r["reliability"] <= 2),
        "by_class": by_class,
    }


def render_report(agg: dict, gen_rows: list[dict], meta: dict) -> str:
    """聚合结果 + 生成层诊断 → Markdown 报告。"""
    tag = meta.get("tag") or "baseline"
    title = (
        "B3 回答质量评测报告（Legal-DC 重叠子集）" if meta.get("tag") else "B3 回答质量基线报告（Legal-DC 重叠子集）"
    )
    n_degraded = sum(1 for g in gen_rows if g.get("degraded"))
    lines = [
        f"# {title}",
        "",
        f"- 评测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行标签：**{tag}**（结果文件 aq_gen{'_' + tag if meta.get('tag') else ''}.jsonl）",
        f"- 数据源：{meta.get('source', 'Legal-DC')} 重叠子集 {meta.get('subset_n', '?')} 条"
        f"（seed={meta.get('seed', '?')} 分层抽样），本轮实际评测前 {meta.get('evaluated_n', '?')} 条"
        "（2026-09-05 基线按 100 条裁剪，断点续跑可随时补至全量）",
        f"- 生成层：{len(gen_rows)} 条有产出，{sum(1 for g in gen_rows if g.get('error'))} 条请求失败，"
        f"{sum(1 for g in gen_rows if g.get('confirmation_required'))} 条触发场景确认未作答"
        + (f"，**{n_degraded} 条由降级后端（Ollama）生成——对比判读前须剔除或重跑**" if n_degraded else ""),
        "",
        "## 一、LLM-as-judge 总评",
        "",
    ]
    if agg.get("n"):
        lines += [
            f"- 有效评判 {agg['n']} 条（解析失败 {agg['failed']} 条）",
            f"- **准确性 {agg['accuracy']} / 5，完整性 {agg['completeness']} / 5，可靠性 {agg['reliability']} / 5**"
            f"（三维总分均值 {agg['total_mean']} / 15）",
            f"- 可靠性低分（≤2）条目：{agg['low_reliability']} 条",
            "",
            "### 三维总分分布",
            "",
            "| 总分 | 条数 |",
            "|------|------|",
        ]
        for score in sorted(agg["score_dist"]):
            lines.append(f"| {score} | {agg['score_dist'][score]} |")
        lines += [
            "",
            "## 二、题型分型",
            "",
            "| 题型 | 条数 | 准确性 | 完整性 | 可靠性 | 总分均值 |",
            "|------|------|--------|--------|--------|----------|",
        ]
        for cls, s in agg["by_class"].items():
            lines.append(
                f"| {cls} | {s['n']} | {s['accuracy']} | {s['completeness']} | {s['reliability']} | {s['overall']} |"
            )
        lines += [
            "",
            "## 三、阶段 2 目标线（对照）",
            "",
            "下表是 2026-09-05 定下的阶段 2 工作目标（团队约定参考线，非路线图硬性标准）：",
            "审核子图/类案子图上线后重跑本管线，逐项对照——达标即证明「多 Agent 优于单 Agent」。",
            "",
            "| 指标 | 基线（本报告） | 阶段 2 目标线 |",
            "|------|----------------|----------------|",
            f"| 三维总分均值 | {agg['total_mean']} / 15 | ≥ 12 / 15 |",
            f"| 可靠性均分 | {agg['reliability']} / 5 | ≥ 4.0 / 5 |",
            f"| 硬失败条数（总分 ≤5） | {sum(v for k, v in agg['score_dist'].items() if k <= 5)} 条 | 减半 |",
            f"| 可靠性低分（≤2）条数 | {agg['low_reliability']} 条 | 显著收缩 |",
            "| 逻辑推理型总分 | 见分型表 | 不落后于整体均分 |",
            "",
            "## 四、评判口径",
            "",
            "- judge 为主 LLM 后端（DeepSeek，temperature=0）；"
            "评分 1-5 整数，三维总分 3-15；解析失败不计入均值。"
            "本基线用于 M4 阶段 2 前后对比（多 Agent 是否优于单 Agent），关注相对变化而非绝对分。",
            "- judge 与被评系统同为 DeepSeek（同源评审），通常偏宽容，可靠性短板只会被低估不会被夸大。",
        ]
    else:
        lines.append("无有效评判结果。")
    return "\n".join(lines) + "\n"


# ============================================================
# 生成层（真实系统调用）
# ============================================================


def stream_chat_full(base_url: str, query: str, top_k: int = 5, timeout: int = 420) -> dict:
    """调 /api/chat/stream，返回 answer/sources/confirmation_required/elapsed。

    与 lexeval_eval.stream_chat 的差异：额外捕获 confirmation_required 事件
    （Legal-DC 部分问题可能触发 B 类场景确认，评测中记为未作答而非报错）。
    """
    url = f"{base_url}/api/chat/stream"
    payload = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = resp.read().decode("utf-8")

    tokens: list[str] = []
    sources: list[dict] = []
    confirmation = False
    degraded = False
    for block in buf.split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            et = ev.get("type")
            if et == "token":
                tokens.append(ev.get("content", ""))
            elif et == "meta":
                sources = ev.get("sources", []) or []
                degraded = bool(ev.get("degraded"))  # 主后端降级（Ollama）出的答案，对比时须可辨识
            elif et == "confirmation_required":
                confirmation = True
    return {
        "answer": "".join(tokens).strip(),
        "sources": sources,
        "confirmation_required": confirmation,
        "degraded": degraded,
        "elapsed": time.time() - t0,
    }


def run_generation(items: list[dict], base_url: str, top_k: int = 5, gen_path: Path = GEN_PATH) -> list[dict]:
    """顺序生成（追加式续跑：跳过已完成 src_id；error 行不算完成，重跑自动补跑）。"""
    gen_path.parent.mkdir(parents=True, exist_ok=True)
    done = done_gen_ids(load_jsonl(gen_path))
    todo = [it for it in items if it["src_id"] not in done]
    print(f"[生成] 已完成 {len(done)} 条，本轮待跑 {len(todo)} 条 → {gen_path.name}")
    results = []
    with open(gen_path, "a", encoding="utf-8") as f:
        for i, item in enumerate(todo):
            row = {
                "src_id": item["src_id"],
                "class": item["class"],
                "query": item["query"],
                "reference_answer": item["reference_answer"],
                "elapsed": 0.0,
                "answer": "",
                "n_sources": 0,
                "error": "",
                "degraded": False,
            }
            try:
                res = stream_chat_full(base_url, item["query"], top_k=top_k)
                row.update(
                    answer=res["answer"],
                    n_sources=len(res["sources"]),
                    elapsed=round(res["elapsed"], 1),
                    confirmation_required=res["confirmation_required"],
                    degraded=res.get("degraded", False),
                )
            except Exception as e:  # 网络抖动/超时记为失败行（不计完成，续跑自动补）
                row["error"] = f"{type(e).__name__}: {e}"
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            results.append(row)
            status = (
                "确认"
                if row.get("confirmation_required")
                else ("错误" if row["error"] else f"{len(row['answer'])}字{'(降级)' if row['degraded'] else ''}")
            )
            print(f"  [{i + 1}/{len(todo)}] #{item['src_id']} {item['class']} {row['elapsed']}s {status}")
    return results


# ============================================================
# 评判层
# ============================================================


def build_judge_prompt(item: dict, answer: str) -> str:
    """拼 judge 提示词：参考答案 + 支撑段落节选 + 系统回答，均截断防爆上下文。"""
    evidence = "\n".join(f"- {str(d)[:300]}" for d in item.get("supporting_documents", [])[:3])
    return JUDGE_PROMPT.format(
        query=item["query"][:500],
        reference=(item.get("reference_answer") or "")[:JUDGE_TEXT_MAX] or "（无）",
        evidence=(evidence or "（无）")[:EVIDENCE_MAX],
        model_output=answer[:JUDGE_TEXT_MAX] or "（空）",
    )


def run_judge(
    gen_rows: list[dict], subset_by_id: dict[int, dict], limit: int, judge_path: Path = JUDGE_PATH
) -> list[dict]:
    """LLM-as-judge（追加式续跑）：对已生成条目打分，结果写 judge_path。

    gen_rows 传入前须 dedupe_last（同 src_id 重试成功后多行，只评最新一条）。
    """
    import src.config  # noqa: F401  # factory 直读 os.getenv，须先经 config 加载 .env
    from src.llm.factory import create_llm_backend  # 延迟导入：--report 不需要 LLM

    llm = create_llm_backend(temperature=0.0)
    judge_path.parent.mkdir(parents=True, exist_ok=True)
    done = done_judge_ids(load_jsonl(judge_path))
    todo = [
        g
        for g in gen_rows
        if g["src_id"] not in done and g.get("answer") and not g.get("error") and not g.get("confirmation_required")
    ]
    if limit > 0:
        todo = todo[:limit]
    print(f"[评判] 已完成 {len(done)} 条，本轮待评 {len(todo)} 条 → {JUDGE_PATH.name}")
    results = []
    with open(JUDGE_PATH, "a", encoding="utf-8") as f:
        for i, row in enumerate(todo):
            item = subset_by_id[row["src_id"]]
            prompt = build_judge_prompt(item, row["answer"])
            out = {
                "src_id": row["src_id"],
                "class": row["class"],
                "accuracy": None,
                "completeness": None,
                "reliability": None,
                "comment": "",
            }
            try:
                resp = llm.chat(prompt, system_prompt=JUDGE_SYSTEM)
                parsed = parse_judge_json(resp)
                if parsed:
                    out.update(
                        accuracy=clamp_score(parsed.get("accuracy")),
                        completeness=clamp_score(parsed.get("completeness")),
                        reliability=clamp_score(parsed.get("reliability")),
                        comment=str(parsed.get("comment", ""))[:200],
                    )
                else:
                    out["comment"] = "JSON 解析失败"
            except Exception as e:
                out["comment"] = f"judge 调用失败: {type(e).__name__}: {e}"
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            results.append(out)
            if (i + 1) % 10 == 0 or i + 1 == len(todo):
                print(f"  评判进度 {i + 1}/{len(todo)}")
    return results


# ============================================================
# Main
# ============================================================


def main() -> None:
    ap = argparse.ArgumentParser(description="B3 回答质量评测（Legal-DC 重叠子集）")
    ap.add_argument("--generate", type=int, default=0, help="生成回答条数（0=跳过）")
    ap.add_argument("--judge", type=int, default=0, help="评判条数（0=跳过；-1=全部已生成）")
    ap.add_argument("--report", action="store_true", help="汇总评判结果出报告")
    ap.add_argument("--tag", default="", help="结果文件标签（对比跑隔离用，如 review → aq_gen_review.jsonl）")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--output", default=str(PROJECT_ROOT / "docs" / "B3-答案质量基线报告.md"))
    args = ap.parse_args()

    gen_path, judge_path = result_paths(args.tag)
    if args.tag:
        print(f"[tag={args.tag}] 生成 → {gen_path.name} | 评判 → {judge_path.name}")

    if args.generate:
        subset = json.loads(SUBSET_PATH.read_text(encoding="utf-8"))
        items = subset["items"][: args.generate]
        run_generation(items, args.base_url, top_k=args.top_k, gen_path=gen_path)

    # 消费端统一去重取最新（error 行重试成功后同 id 多行）
    gen_rows = dedupe_last(load_jsonl(gen_path))
    if args.judge != 0:
        subset = json.loads(SUBSET_PATH.read_text(encoding="utf-8"))
        subset_by_id = {it["src_id"]: it for it in subset["items"]}
        run_judge(gen_rows, subset_by_id, limit=args.judge if args.judge > 0 else 10**9, judge_path=judge_path)

    if args.report:
        judged = dedupe_last(load_jsonl(judge_path))
        agg = aggregate(judged)
        subset = json.loads(SUBSET_PATH.read_text(encoding="utf-8"))
        report = render_report(
            agg,
            gen_rows,
            meta={
                "source": subset.get("source"),
                "seed": subset.get("seed"),
                "subset_n": len(subset["items"]),
                "evaluated_n": len(gen_rows),
                "tag": args.tag,
            },
        )
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"[报告] {out}")
        print(report[:800])


if __name__ == "__main__":
    main()
