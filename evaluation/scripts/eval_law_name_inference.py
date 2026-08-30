"""法名推断准确率评测（B2 spike 第二步）：gold 法名是否进向量最近邻 top-k。

在无法名口语集（eval_queries_colloq_llm.json，148 条）上：查询 embed →
与 991 部法律描述向量做余弦最近邻 → gold 法名的 Recall@1/3/5/10 与 MRR。

判据（go/no-go）：
  - gold 法名 Recall@3 显著高于随机（1/991≈0.1%）且达到可用水位（≥60%），
    才值得做第三步（软信号接入检索、端到端跑 eval_retrieval 对比 73%）；
  - 对照天花板：precise 集（带法名）97.5%——法名信号接入后的理想收益参照。

用法:
  uv run python evaluation/scripts/eval_law_name_inference.py                # colloq148 集
  uv run python evaluation/scripts/eval_law_name_inference.py --queries ...  # 其他集
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("eval_law_name_inference")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(ROOT))

NPZ = EVAL_DIR / "data" / "law_name_index.npz"
META = EVAL_DIR / "data" / "law_name_index.json"
RESULTS_DIR = EVAL_DIR / "data" / "lexeval" / "results"


def norm_law(name: str) -> str:
    s = re.sub(r"[（(][^）)]*[）)]", "", name or "")
    return re.sub(r"^中华人民共和国", "", s).strip()


def centroid_vectors() -> tuple[list[str], np.ndarray]:
    """变体 2：每法条文向量质心（复用 pgvector 已有向量，零 embed 成本）。

    描述文本法与查询的相似度可能偏"字面"，质心是真实条文内容的分布中心，
    与检索本身的路由行为更一致——两者对比取优。
    """
    import psycopg2
    from src.config import PG_CONN

    conn = psycopg2.connect(PG_CONN)
    sums: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata->>'law_name', embedding FROM document_chunks "
            "WHERE chunk_type='article' AND metadata->>'law_name' IS NOT NULL"
        )
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for law_name, emb in rows:
                v = np.asarray([float(x) for x in str(emb).strip("[]").split(",")], dtype=np.float32)
                if law_name not in sums:
                    sums[law_name] = np.zeros_like(v)
                    counts[law_name] = 0
                sums[law_name] += v
                counts[law_name] += 1
    conn.close()
    laws = sorted(sums)
    mat = np.stack([sums[l] / max(counts[l], 1) for l in laws])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return laws, mat / norms


def main():
    ap = argparse.ArgumentParser(description="法名向量最近邻推断评测")
    ap.add_argument("--queries", default=str(EVAL_DIR / "data" / "eval_queries_colloq_llm.json"))
    ap.add_argument("--tag", default="", help="报告文件名后缀")
    ap.add_argument("--mode", choices=["desc", "centroid"], default="desc",
                    help="desc=描述文本向量（需先建索引）；centroid=条文向量质心（直连 PG，零 embed）")
    args = ap.parse_args()

    if args.mode == "centroid":
        law_names, vectors = centroid_vectors()
        law_norm = [norm_law(l) for l in law_names]
        logger.info(f"质心索引 {vectors.shape[0]} 部 dim={vectors.shape[1]}（复用 PG 已有向量）")
    else:
        vectors = np.load(NPZ)["vectors"]  # (L, D) 已 L2 归一化
        meta = json.loads(META.read_text(encoding="utf-8"))
        law_norm = [norm_law(m["law_name"]) for m in meta]
        logger.info(f"描述文本索引 {vectors.shape[0]} 部 dim={vectors.shape[1]}")

    queries = [json.loads(l) for l in Path(args.queries).read_text(encoding="utf-8").splitlines() if l.strip()]
    logger.info(f"评测集 {len(queries)} 条")

    from src.api import dependencies  # noqa: E402

    embedder = dependencies._create_embedder()
    logger.info(f"embedder: {embedder.model}")

    t0 = time.time()
    q_texts = [q["query"] for q in queries]
    q_vecs = np.asarray(embedder.embed_documents(q_texts), dtype=np.float32)
    norms = np.linalg.norm(q_vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    q_vecs = q_vecs / norms
    logger.info(f"查询向量化完成（{time.time() - t0:.1f}s）")

    # 相似度矩阵（N×L）+ 排名
    sims = q_vecs @ vectors.T
    order = np.argsort(-sims, axis=1)  # 每行按相似度降序的法律索引

    hits = {1: 0, 3: 0, 5: 0, 10: 0}
    rr_sum = 0.0
    details = []
    for i, q in enumerate(queries):
        gold = norm_law(q["relevant"][0]["law_name"])
        ranked = [law_norm[j] for j in order[i]]
        rank = next((r for r, name in enumerate(ranked, 1) if name == gold), None)
        for k in (1, 3, 5, 10):
            if rank is not None and rank <= k:
                hits[k] += 1
        rr = 1.0 / rank if rank else 0.0
        rr_sum += rr
        details.append({"id": q["id"], "query": q["query"], "gold": gold, "rank": rank, "mrr": rr})
        flag = f"rank={rank}" if rank else "MISS"
        logger.info(f"[{i + 1:>3}/{len(queries)}] {flag:>6} | {q['query'][:32]} → {ranked[0]}")

    n = len(queries)
    mrr = rr_sum / n
    mode_tag = args.tag or Path(args.queries).stem.replace("eval_queries_", "")
    lines = [
        "=" * 72,
        f"法名向量最近邻推断 | 评测集: {Path(args.queries).name} | {time.strftime('%Y-%m-%d %H:%M')}",
        f"查询数: {n} | 法名库: {len(law_norm)} 部 | embedder: {embedder.model}",
        "-" * 72,
        f"{'指标':<12}{'gold 法名进入 top-k 的比例':>26}",
        *[f"{'Recall@' + str(k):<12}{hits[k] / n:>26.1%}" for k in (1, 3, 5, 10)],
        f"{'MRR':<12}{mrr:>26.4f}",
        "-" * 72,
        f"随机基线 Recall@1≈{1 / len(law_norm):.2%}（{len(law_norm)} 部法律）",
        f"gold 不在 top10: {sum(1 for d in details if d['rank'] is None or d['rank'] > 10)}/{n} 条",
        f"gold 完全无匹配(norm 不一致): {sum(1 for d in details if d['rank'] is None)}/{n} 条",
        *[f"  rank>10: {d['query'][:36]}（gold={d['gold']}）"
          for d in details if d["rank"] is not None and d["rank"] > 10][:15],
    ]
    report = "\n".join(lines)
    print("\n" + report)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"law_name_inference_{mode_tag}.txt"
    out.write_text(report + "\n", encoding="utf-8")
    (RESULTS_DIR / f"law_name_inference_{mode_tag}.jsonl").write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in details), encoding="utf-8"
    )
    logger.info(f"报告已保存: {out}")


if __name__ == "__main__":
    main()
