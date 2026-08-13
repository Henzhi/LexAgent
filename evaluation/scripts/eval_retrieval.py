"""检索质量评测：基于 data/eval_queries.json（100 条）计算 Hit@k / MRR。

直接调用本进程内的检索器（pgvector），不经过 HTTP 与 LLM，聚焦检索本身。

用法:
  python scripts/eval_retrieval.py                   # 生产配置（reranker+adjacent+阈值）
  python scripts/eval_retrieval.py --bare            # 纯 pgvector 粗排（消融对比）
  python scripts/eval_retrieval.py --top-k 10

指标说明（查询级）:
  Hit@k : top-k 中至少命中一条标注相关条文的查询占比
  MRR   : 第一条命中结果的排名倒数均值
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# Windows 控制台 GBK 兼容
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# evaluation/scripts/eval_retrieval.py -> EVAL_DIR=evaluation/, ROOT=项目根
EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
QUERIES_PATH = EVAL_DIR / "data" / "eval_queries.json"

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(s: str) -> int:
    """中文数字 → 整数：'十'→10, '十七'→17, '一百三十三'→133, '一千零八十七'→1087"""
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            num = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            section += (num or 1) * CN_UNITS[ch]
            num = 0
    return section + num


def article_numbers(article_range: str) -> set[int]:
    """解析 article_range 中的条款号集合：'第十条'→{10}, '第十条至第十二条'→{10,11,12}"""
    nums: set[int] = set()
    if not article_range:
        return nums
    for m in re.finditer(
        r"第([零一二两三四五六七八九十百千]+)条"
        r"(?:\s*(?:至|到|、)\s*第([零一二两三四五六七八九十百千]+)条)?",
        article_range,
    ):
        a = cn_to_int(m.group(1))
        nums.add(a)
        if m.group(2):
            b = cn_to_int(m.group(2))
            nums.update(range(a, b + 1))
    return nums


def law_match(rel_law: str, res_law: str) -> bool:
    if not rel_law or not res_law:
        return False
    if rel_law == res_law:
        return True
    return rel_law in res_law or res_law in rel_law


def hit_one(rel: dict, results: list) -> bool:
    """单条标注是否命中 top-k（results 元素可为 dict 或 RetrievedDoc）"""
    target = {cn_to_int(str(rel.get("article_number", "")))}
    for r in results:
        r_law = r.get("law_name", "") if isinstance(r, dict) else getattr(r, "law_name", "")
        r_range = r.get("article_range", "") if isinstance(r, dict) else getattr(r, "article_range", "")
        if law_match(rel.get("law_name", ""), r_law):
            if article_numbers(r_range) & target:
                return True
    return False


def build_retriever(bare: bool):
    """构建检索器：bare=False 用生产配置（reranker+adjacent+阈值）"""
    sys.path.insert(0, str(ROOT))
    from src.api import dependencies

    embedder = dependencies._create_embedder()
    if bare:
        from src.knowledge.pgvector_store import PgvectorStore
        from src.rag.retriever import PgvectorStoreRetriever
        from src.config import PG_CONN

        store = PgvectorStore(PG_CONN)
        store.ensure_tables()
        return PgvectorStoreRetriever(
            store=store, embedder=embedder, embedding_model=embedder.model
        ), embedder
    return dependencies._create_retriever(embedder), embedder


def main():
    ap = argparse.ArgumentParser(description="检索质量评测 (eval_queries.json)")
    ap.add_argument("--top-k", type=int, default=15, help="检索返回条数（默认 15，含精排扩容）")
    ap.add_argument("--bare", action="store_true", help="纯 pgvector 粗排（不含 reranker/adjacent/阈值）")
    ap.add_argument("--limit", type=int, default=0, help="只评测前 N 条（0=全部）")
    ap.add_argument("--queries", default=str(QUERIES_PATH), help="评测集文件（JSON 或 JSONL）")
    args = ap.parse_args()

    qpath = Path(args.queries)
    with open(qpath, encoding="utf-8") as f:
        raw_lines = [line for line in f if line.strip()]
    # 兼容 JSON 与 JSONL：整文件能解析则用 JSON，否则按行解析
    try:
        queries = json.loads("".join(raw_lines))
    except json.JSONDecodeError:
        queries = [json.loads(line) for line in raw_lines]
    if args.limit:
        queries = queries[: args.limit]

    print(f"评测配置: {'BARE 纯向量' if args.bare else '生产配置(纯向量+reranker+adjacent+sim阈值)'} | "
          f"查询数: {len(queries)} | top_k={args.top_k}")
    print("-" * 76)

    retriever, embedder = build_retriever(args.bare)
    print(f"检索器就绪: {type(retriever).__name__} | embedder={embedder.model}")

    # 评测
    hits = {1: 0, 3: 0, 5: 0, 10: 0}
    rr_sum = 0.0
    details = []
    t0 = time.time()

    for i, q in enumerate(queries, 1):
        results = retriever.search(q["query"], top_k=args.top_k)
        rels = q.get("relevant", [])
        # 各 k 是否命中
        hit_flags = {}
        for k in (1, 3, 5, 10):
            hit_flags[k] = any(hit_one(rel, results[:k]) for rel in rels)
            if hit_flags[k]:
                hits[k] += 1
        # MRR：第一个命中的排名
        rr = 0.0
        for rank, r in enumerate(results, 1):
            if any(hit_one(rel, [r]) for rel in rels):
                rr = 1.0 / rank
                break
        rr_sum += rr
        if results:
            top_citation = getattr(results[0], "citation", "") if not isinstance(results[0], dict) \
                else f"{results[0].get('law_name','')} · {results[0].get('article_range','')}"
        else:
            top_citation = ""
        details.append({
            "id": q.get("id"),
            "query": q["query"],
            "hit@5": hit_flags[5],
            "mrr": round(rr, 4),
            "top_citation": top_citation,
            "n_rel": len(rels),
        })
        print(f"[{i:>3}/{len(queries)}] {'Y' if hit_flags[5] else 'N'} "
              f"hit@1={hit_flags[1]} hit@5={hit_flags[5]} mrr={rr:.3f} | {q['query'][:30]}")

    elapsed = time.time() - t0
    n = len(queries)
    mode = "BARE" if args.bare else "PROD"

    baselines = {1: 0.52, 3: 0.65, 5: 0.73, 10: 0.81}
    mrr = rr_sum / n
    miss5 = [d for d in details if not d["hit@5"]]

    lines = []
    lines.append("=" * 76)
    lines.append(f"评测时间: {time.strftime('%Y-%m-%d %H:%M:%S')} | 配置: {mode}")
    lines.append(f"查询数: {n} | 总耗时: {elapsed:.1f}s | 平均: {elapsed / max(n, 1):.2f}s/查询")
    lines.append("-" * 76)
    lines.append(f"{'指标':<10}{'本系统':<10}{'旧FAISS基线':<14}{'差值'}")
    for k in (1, 3, 5, 10):
        cur = hits[k] / n
        bl = baselines[k]
        lines.append(f"{'Hit@' + str(k):<10}{cur:<10.1%}{bl:<14.1%}{cur - bl:+.1%}")
    lines.append(f"{'MRR':<10}{mrr:<10.4f}{'0.6113':<14}{mrr - 0.6113:+.4f}")
    lines.append("-" * 76)
    lines.append(f"\nHit@5 未命中 {len(miss5)}/{n} 条:")
    for d in miss5:
        lines.append(f"  #{d['id']:<4} {d['query'][:50]}")

    report = "\n".join(lines)
    print("\n" + report)

    # 保存 UTF-8 报告 + 明细（绕开 Windows 控制台重定向的编码问题）
    out_dir = EVAL_DIR / "data" / "lexeval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"retrieval_{'bare' if args.bare else 'prod'}.txt"
    report_path.write_text(report + "\n", encoding="utf-8")
    detail_path = out_dir / f"retrieval_{'bare' if args.bare else 'prod'}.jsonl"
    with open(detail_path, "w", encoding="utf-8") as f:
        for d in details:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"\n报告已保存(UTF-8): {report_path}")
    print(f"明细已保存: {detail_path}")


if __name__ == "__main__":
    main()
