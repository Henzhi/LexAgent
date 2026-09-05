"""B3 答案质量评测数据准备：从 Legal-DC 基准筛与 LexAgent 知识库重叠的 QA 子集。

背景（见 docs/B3 与记忆 legal-dc-benchmark）：
  - Legal-DC（github.com/legal-dc/Legal-DC）pro_LawQA.json 共 2474 条带参考答案 QA，
    每条含 query / answer（参考答案）/ document（支撑段落）/ title（出处规章）/ class（题型）。
  - 该仓库**无 LICENSE**：克隆源与衍生数据集一律不入库（.gitignore 已覆盖
    evaluation/data/legal_dc/），本脚本只提交代码；发布/商用前需谨慎评估。

流程：
  1. 加载 Legal-DC pro_LawQA.json（本地克隆，snapshot 锁 commit）；
  2. 加载 LexAgent 知识库法名清单（law_name_index.json，与既有评测口径一致）；
  3. 法名归一化后求交集 → 重叠 QA 子集；
  4. 按题型分层抽样 N 条（默认 150，seed 固定可复现）→ answer_quality_subset.json。

用法:
  uv run python evaluation/scripts/build_legal_dc_dataset.py               # 全量重叠统计
  uv run python evaluation/scripts/build_legal_dc_dataset.py --sample 150  # 统计 + 抽样落盘
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台 GBK 兜底
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = EVAL_DIR.parent

# Legal-DC 克隆位置与 snapshot（无 LICENSE，锁版本防上游漂移；2026-09-05 克隆时 HEAD）
LEGAL_DC_DIR = EVAL_DIR / "data" / "legal_dc" / "legal-dc-src"
LEGAL_DC_SNAPSHOT = "1f4422e4a9de4ce7bb311ebf926f61c1b077d50a"
PRO_LAWQA_PATH = LEGAL_DC_DIR / "Legal-DC" / "data" / "pro_LawQA.json"
CLONE_URL = "https://github.com/legal-dc/Legal-DC"

KB_INDEX_PATH = EVAL_DIR / "data" / "law_name_index.json"
OUT_PATH = EVAL_DIR / "data" / "legal_dc" / "answer_quality_subset.json"

# 请求体上限：ChatRequest.query ≤2000 字符（lexeval_eval 同款安全余量）
MAX_QUERY_LEN = 1900

# 题型归一化：原始数据存在前导空格（如 " 逻辑推理型"）
KNOWN_CLASSES = ("概括归纳型", "逻辑推理型", "概念解释型", "其他")


def ensure_legal_dc() -> None:
    """确认 Legal-DC 克隆就位并校验 snapshot；缺失时给出克隆指引后退出。"""
    if not PRO_LAWQA_PATH.exists():
        print(f"[Legal-DC] 克隆源缺失，请执行：\n  git clone --depth 1 {CLONE_URL} {LEGAL_DC_DIR}")
        raise SystemExit(1)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=LEGAL_DC_DIR, capture_output=True, text=True).stdout.strip()
    if head and head != LEGAL_DC_SNAPSHOT:
        print(
            f"[Legal-DC] ⚠️ snapshot 漂移：本地 {head[:12]} ≠ 锁定 {LEGAL_DC_SNAPSHOT[:12]}（无 LICENSE 仓库，注意上游变更）"
        )


def normalize_title(title: str) -> str:
    """规章/法典名归一化：去书名号与空白、全半角括号统一、去版本年份后缀。

    《肉制品生产许可审查细则（2023版）》 → 肉制品生产许可审查细则
    中华人民共和国刑法(2023修正)        → 中华人民共和国刑法
    """
    s = (title or "").strip().strip("《》").strip()
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s)
    # 版本/修订标记整体去掉：(2023版) / (2023修正) / (2021年修订) / (1997修订) 等
    s = re.sub(r"\(\d{4}[^)]*\)$", "", s)
    return s


def normalize_class(raw: str) -> str:
    """题型归一化：原始数据有前导空格；未知题型并入「其他」。"""
    c = (raw or "").strip()
    return c if c in KNOWN_CLASSES else "其他"


def load_kb_names(index_path: Path) -> set[str]:
    """知识库法名归一化集合：全名与短名都收（Legal-DC 可能用任一形式引用）。"""
    index = json.loads(index_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for row in index:
        for key in ("law_name", "short"):
            v = normalize_title(row.get(key, ""))
            if v:
                names.add(v)
    return names


def load_pro_lawqa(path: Path) -> list[dict]:
    """读取 pro_LawQA.json（JSON 数组），做最小清洗（题型归一化、去空行）。"""
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, r in enumerate(rows):
        query = (r.get("query") or "").strip()
        answer = (r.get("answer") or "").strip()
        if not query or not answer:
            continue
        out.append(
            {
                "src_id": i,
                "query": query[:MAX_QUERY_LEN],
                "reference_answer": answer,
                "supporting_documents": [d for d in (r.get("document") or []) if str(d).strip()],
                "law_title": (r.get("title") or "").strip(),
                "class": normalize_class(r.get("class", "")),
            }
        )
    return out


def build_overlap(rows: list[dict], kb_names: set[str]) -> list[dict]:
    """出处法名归一化后命中知识库的 QA 子集（重叠 = 系统具备该规章的全文）。"""
    return [r for r in rows if normalize_title(r["law_title"]) in kb_names]


def stratified_sample(items: list[dict], n: int, seed: int = 42) -> list[dict]:
    """按题型分层等比抽样；某层超出配额上限时按比例取整、余数给大层（保证恰好 n 条）。"""
    if n >= len(items):
        return list(items)
    by_class: dict[str, list[dict]] = collections.defaultdict(list)
    for it in items:
        by_class[it["class"]].append(it)
    rng = random.Random(seed)
    for group in by_class.values():
        rng.shuffle(group)
    quota = {c: len(g) * n / len(items) for c, g in by_class.items()}
    picked: list[dict] = []
    for c, q in quota.items():
        picked.extend(by_class[c][: round(q)])
    # 取整误差回填：从尚未入选的剩余池按层补齐
    remaining = {c: by_class[c][round(quota[c]) :] for c in by_class}
    fill_order = sorted(by_class, key=lambda c: -(quota[c] - round(quota[c])))
    for c in fill_order:
        while len(picked) < n and remaining[c]:
            picked.append(remaining[c].pop(0))
    rng.shuffle(picked)
    return picked[:n]


def main() -> None:
    ap = argparse.ArgumentParser(description="B3 答案质量评测数据准备（Legal-DC 重叠子集）")
    ap.add_argument("--sample", type=int, default=0, help="抽样条数（0=只做重叠统计不落盘）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    ensure_legal_dc()
    rows = load_pro_lawqa(PRO_LAWQA_PATH)
    kb_names = load_kb_names(KB_INDEX_PATH)
    print(f"[数据] Legal-DC 有效 QA {len(rows)} 条 | 知识库法名（归一化）{len(kb_names)} 个")

    overlap = build_overlap(rows, kb_names)
    n_laws = len({normalize_title(r["law_title"]) for r in overlap})
    print(f"[重叠] 命中 {n_laws} 部规章/法律，QA {len(overlap)}/{len(rows)} = {len(overlap) / len(rows) * 100:.1f}%")
    for c, k in collections.Counter(r["class"] for r in overlap).most_common():
        print(f"    {c}: {k} 条")

    if args.sample <= 0:
        return

    subset = stratified_sample(overlap, args.sample, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "source": f"legal-dc/Legal-DC@{LEGAL_DC_SNAPSHOT[:12]}",
                "seed": args.seed,
                "overlap_total": len(overlap),
                "items": subset,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"[抽样] {len(subset)} 条 → {args.output}")
    for c, k in collections.Counter(r["class"] for r in subset).most_common():
        print(f"    {c}: {k} 条")


if __name__ == "__main__":
    main()
