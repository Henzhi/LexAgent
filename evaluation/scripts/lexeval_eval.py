"""LexEval 基准评估脚本：通过 /api/chat/stream 流式接口批量问答，机判选择题准确率。

数据: data/lexeval/{task}.json（JSONL，每行 {instruction, input, answer}）
     可从 GitHub 下载: https://github.com/CSHaitao/LexEval/tree/main/data

用法:
  python scripts/lexeval_eval.py --task 1_1,3_1 --limit 5 --seed 42
  python scripts/lexeval_eval.py --task all --limit 3
  python scripts/lexeval_eval.py --task 3_1 --limit 0      # 0 = 该任务全部
  python scripts/lexeval_eval.py --task 1_1 --limit 5 --base-url http://127.0.0.1:8000

输出:
  data/lexeval/results/{task}_top{limit}.jsonl   每题明细（追加模式）
  data/lexeval/results/summary.json              汇总指标
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

# Windows 控制台可能 GBK 编码，强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
DATA_DIR = EVAL_DIR / "data" / "lexeval"
OUT_DIR = EVAL_DIR / "data" / "lexeval" / "results"

ALL_TASKS = [f"{i}_{j}" for i in range(1, 7) for j in range(1, 10)]  # 实际以文件为准

# LexCog 能力分类
TASK_META = {
    1: "记忆/法条回忆",
    2: "理解/要素识别",
    3: "逻辑推理/案例分析",
    4: "辨别/案例检索",
    5: "生成/翻译",
    6: "伦理",
}

LETTER_RE = re.compile(r"[A-D]+")
# 后端 ChatRequest.query 上限 2000 字符，留安全余量
MAX_QUERY_LEN = 1900


def load_task(task: str) -> list[dict]:
    """读取 JSONL 任务文件"""
    path = DATA_DIR / f"{task}.json"
    if not path.exists():
        raise FileNotFoundError(f"任务文件不存在: {path}（先下载 LexEval 数据到 data/lexeval/）")
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def is_machine_grable(row: dict) -> bool:
    """是否可机判：标准答案为 A-D 字母组合"""
    return bool(re.fullmatch(r"[A-D]+", row.get("answer", "").strip().upper()))


def extract_letters(text: str) -> str:
    """从回答中提取选项字母。

    模型输出常见格式: "B: 根据《X法》第X条..." / "答案为ABD" / "A/B/C"
    优先级: ①"答案(为|是)"字样后 ②回答开头的字母组 ③第一个字母组
    """
    text = text or ""
    m = re.search(r"答案(?:为|是)?[:：]?\s*([A-D]+)", text)
    if m:
        return "".join(dict.fromkeys(m.group(1)))
    m = re.match(r"^\s*[A-D]+(?:[/、\s]?[A-D])*", text)
    if m:
        return "".join(dict.fromkeys(re.sub(r"[^A-D]", "", m.group(0))))
    m = LETTER_RE.search(text)
    return "".join(dict.fromkeys(m.group(0))) if m else ""


def grade(pred: str, gold: str) -> bool:
    """判定：字母集合相同即正确"""
    return "".join(sorted(pred)) == "".join(sorted(gold))


def stream_chat(base_url: str, query: str, top_k: int = 5, timeout: int = 300) -> dict:
    """调用 /api/chat/stream，返回 {answer, sources, events}"""
    url = f"{base_url}/api/chat/stream"
    payload = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = resp.read().decode("utf-8")

    tokens: list[str] = []
    sources: list[dict] = []
    is_casual = False
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
                is_casual = bool(ev.get("is_casual"))
    return {
        "answer": "".join(tokens).strip(),
        "sources": sources,
        "is_casual": is_casual,
        "elapsed": time.time() - t0,
    }


def build_query(row: dict) -> str:
    """构造提交给后端的 query。超长时保留尾部（问题与选项在末尾，最紧要）。"""
    query = (row.get("instruction", "").strip() + "\n" + row.get("input", "")).strip()
    if len(query) > MAX_QUERY_LEN:
        query = query[-MAX_QUERY_LEN:]
    return query


def run_one(
    base_url: str, row: dict, top_k: int = 5, max_retries: int = 1
) -> dict:
    """对单条题目做一次问答（失败可重试）"""
    query = build_query(row)
    for attempt in range(max_retries + 1):
        try:
            res = stream_chat(base_url, query, top_k=top_k)
            gold = row.get("answer", "").strip().upper()
            pred = extract_letters(res["answer"])
            return {
                "task": row.get("_task", ""),
                "query": query[:300],
                "gold": gold,
                "gold_raw": row.get("answer", ""),
                "pred": pred,
                "correct": grade(pred, gold),
                "answer_len": len(res["answer"]),
                "answer_preview": res["answer"][:120],
                "n_sources": len(res["sources"]),
                "top_law": res["sources"][0].get("law_name", "") if res["sources"] else "",
                "top_citation": res["sources"][0].get("citation", "") if res["sources"] else "",
                "is_casual": res["is_casual"],
                "elapsed": res["elapsed"],
                "retries": attempt,
            }
        except Exception as e:
            if attempt >= max_retries:
                return {
                    "task": row.get("_task", ""),
                    "query": query[:300],
                    "gold": row.get("answer", "").strip().upper(),
                    "gold_raw": row.get("answer", ""),
                    "pred": "",
                    "correct": False,
                    "answer_len": 0,
                    "answer_preview": f"ERROR: {type(e).__name__}: {e}",
                    "n_sources": 0,
                    "top_law": "",
                    "top_citation": "",
                    "is_casual": False,
                    "elapsed": 0,
                    "retries": attempt + 1,
                }
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description="LexEval 基准评估")
    ap.add_argument("--task", default="all", help="任务名(逗号分隔)或 all")
    ap.add_argument("--limit", type=int, default=5, help="每任务抽样条数，0=全部")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（固定可复现）")
    ap.add_argument("--top-k", type=int, default=5, help="检索条数")
    ap.add_argument("--base-url", default="http://127.0.0.1:8001", help="后端地址")
    ap.add_argument("--dry-run", action="store_true", help="只统计题目分布不请求")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 收集任务
    if args.task == "all":
        tasks = sorted(
            p.stem for p in DATA_DIR.glob("*.json")
            if re.fullmatch(r"\d+_\d+", p.stem)
        )
    else:
        tasks = [t.strip() for t in args.task.split(",") if t.strip()]
    if not tasks:
        print(f"[!] data/lexeval/ 下没有任务文件，请先下载。参考: ")
        print("    https://raw.githubusercontent.com/CSHaitao/LexEval/main/data/1_1.json")
        sys.exit(1)

    print(f"后端: {args.base_url} | 任务: {', '.join(tasks)} | 每任务抽样: {args.limit or '全部'} | seed={args.seed}")
    print("-" * 70)

    summary_rows: list[dict] = []
    all_details = []
    total_elapsed = 0.0

    for task in tasks:
        rows = load_task(task)
        grable = [r for r in rows if is_machine_grable(r)]
        if not grable:
            print(f"[skip] {task}: 无可机判题目（共 {len(rows)} 条，均无 A-D 字母答案）")
            continue

        if args.dry_run:
            print(f"[{task}] 共 {len(rows)} 条, 可机判 {len(grable)} 条")
            continue

        rng = random.Random(args.seed)
        sample = grable if args.limit == 0 else rng.sample(grable, min(args.limit, len(grable)))
        for r in sample:
            r["_task"] = task

        correct = 0
        with_sources = 0
        task_elapsed = 0.0
        detail_rows = []
        for i, row in enumerate(sample, 1):
            rec = run_one(args.base_url, row, top_k=args.top_k)
            detail_rows.append(rec)
            all_details.append(rec)
            correct += 1 if rec["correct"] else 0
            with_sources += 1 if rec["n_sources"] > 0 else 0
            task_elapsed += rec["elapsed"]
            mark = "✓" if rec["correct"] else "✗"
            print(
                f"[{task} {i}/{len(sample)}] {mark} 预测={rec['pred'] or '∅'} "
                f"标准={rec['gold']} 检索源={rec['n_sources']} "
                f"耗时={rec['elapsed']:.1f}s"
            )
            if not rec["correct"]:
                print(f"       ↳ 回答前80字: {rec['answer_preview'][:80]}")

        acc = correct / len(sample) if sample else 0
        hit = with_sources / len(sample) if sample else 0
        total_elapsed += task_elapsed
        summary_rows.append({
            "task": task,
            "capability": TASK_META.get(int(task.split("_")[0]), ""),
            "n": len(sample),
            "correct": correct,
            "accuracy": round(acc, 4),
            "hit_rate": round(hit, 4),
            "avg_elapsed": round(task_elapsed / len(sample), 1) if sample else 0,
        })
        print(f"  => {task} acc={acc:.1%} ({correct}/{len(sample)}) 检索命中={hit:.1%} 平均耗时={task_elapsed/max(len(sample),1):.1f}s")
        print("-" * 70)

        # 写明细
        out_jsonl = OUT_DIR / f"{task}_top{args.limit}.jsonl"
        with open(out_jsonl, "a", encoding="utf-8") as f:
            for rec in detail_rows:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if args.dry_run:
        return

    # 汇总
    summary = {
        "base_url": args.base_url,
        "seed": args.seed,
        "top_k": args.top_k,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_elapsed_sec": round(total_elapsed, 1),
        "tasks": summary_rows,
    }
    n_all = sum(t["n"] for t in summary_rows)
    acc_all = sum(t["correct"] for t in summary_rows) / max(n_all, 1)
    hit_all = sum(t["n"] * t["hit_rate"] for t in summary_rows) / max(n_all, 1)
    print("\n===== 汇总 =====")
    print(f"{'任务':<6}{'能力':<12}{'题数':<5}{'正确':<5}{'准确率':<8}{'检索命中':<9}{'均耗时'}")
    for t in summary_rows:
        print(
            f"{t['task']:<6}{t['capability']:<12}{t['n']:<5}{t['correct']:<5}"
            f"{t['accuracy']:.1%}    {t['hit_rate']:.1%}      {t['avg_elapsed']:.1f}s"
        )
    print(f"合计: {n_all} 题, 整体准确率 {acc_all:.1%}, 检索命中率 {hit_all:.1%}, 总耗时 {total_elapsed/60:.1f} 分钟")

    with open(OUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n明细已保存: {OUT_DIR}")


if __name__ == "__main__":
    main()
