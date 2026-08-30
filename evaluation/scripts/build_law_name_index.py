"""构建「法名向量索引」（B2 法名推断 spike，第一步，零 LLM 成本）。

思路：把库里每部法律变成一个向量（法名 + 该法条文池的 TF≥2 核心概念词
拼成的描述文本），在线查询时用主检索本来就要算的查询向量做最近邻，
top2~3 候选法名作为软信号（绝不硬过滤）。

为什么 embed 描述文本而不是裸法名：裸法名太短（"劳动合同法"），与长
口语查询的向量相似度不稳；描述文本补上"这部法讲什么"的语义内容。
关键词复用 generate_broad_queries 的 pick_keywords / build_law_df
（TF≥2 + law DF 稀有度，8/29 调好的"核心概念"逻辑），零 LLM。

产物:
  evaluation/data/law_name_index.npz   向量矩阵（L2 归一化）
  evaluation/data/law_name_index.json  法名/简称/描述文本元数据

用法:
  uv run python evaluation/scripts/build_law_name_index.py
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("build_law_name_index")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR / "scripts"))
sys.path.insert(0, str(ROOT))

OUT_NPZ = EVAL_DIR / "data" / "law_name_index.npz"
OUT_JSON = EVAL_DIR / "data" / "law_name_index.json"

_ARTICLE_CD = re.compile(r"第([零一二两三四五六七八九十百千]+)条")


def short_name(law_name: str) -> str:
    s = re.sub(r"[（(][^）)]*[）)]", "", law_name or "").strip()
    s = re.sub(r"^中华人民共和国", "", s)
    return s or law_name


def main():
    ap = argparse.ArgumentParser(description="法名向量索引构建（本地 embed，零 API 费用）")
    ap.add_argument("--max-articles-per-law", type=int, default=40,
                    help="每部法律最多取多少条条文做关键词抽取（条文多的法律抽样足够）")
    ap.add_argument("--top-kw", type=int, default=14, help="描述文本保留的核心概念词数")
    args = ap.parse_args()

    from src.config import PG_CONN
    from src.knowledge.pgvector_store import PgvectorStore
    from generate_broad_queries import build_law_df, pick_keywords  # noqa: E402

    store = PgvectorStore(PG_CONN)
    store.ensure_tables()
    t0 = time.time()
    with store._conn.cursor() as cur:
        cur.execute(
            "SELECT dc.metadata->>'law_name', dc.metadata->>'article_range', dc.content "
            "FROM document_chunks dc JOIN documents d ON dc.doc_id = d.id "
            "WHERE d.status='active' AND dc.chunk_type='article'"
        )
        rows = cur.fetchall()
    logger.info(f"读取 {len(rows)} 条条文（{time.time() - t0:.1f}s）")

    # 按法聚合条文（过滤碎片，与 broad 生成器同口径）
    by_law: dict[str, list[str]] = {}
    for law_name, article_range, content in rows:
        if not law_name or not _ARTICLE_CD.match(article_range or ""):
            continue
        if not content or len(content) < 30:
            continue
        by_law.setdefault(law_name, []).append(content)
    logger.info(f"聚合 {len(by_law)} 部法律")

    # 每法核心概念词：对抽样条文逐条 pick_keywords（law DF 稀有度 + TF≥2），聚合词频
    law_df = build_law_df({k: [{"content": c} for c in v] for k, v in by_law.items()})
    law_meta: list[dict] = []
    for law_name, contents in by_law.items():
        short = short_name(law_name)
        sample = contents[: args.max_articles_per_law]
        kw_count: collections.Counter = collections.Counter()
        for content in sample:
            for w in pick_keywords(content, law_df, law_name, top_k=3, max_law_df=25, min_tf=2):
                kw_count[w] += 1
        top_kw = [w for w, _ in kw_count.most_common(args.top_kw)]
        desc = f"{law_name} {short} " + " ".join(top_kw)
        law_meta.append({
            "law_name": law_name,
            "short": short,
            "keywords": top_kw,
            "n_articles": len(contents),
            "desc": desc.strip(),
        })
    logger.info(f"描述文本构造完成（{len(law_meta)} 部）")

    # 向量化（与查询同一 embedder → 同一向量空间）
    from src.api import dependencies  # noqa: E402

    embedder = dependencies._create_embedder()
    logger.info(f"embedder: {embedder.model}")
    t1 = time.time()
    texts = [m["desc"] for m in law_meta]
    vectors = np.asarray(embedder.embed_documents(texts), dtype=np.float32)
    logger.info(f"embed {len(texts)} 条（{time.time() - t1:.1f}s）dim={vectors.shape[1]}")

    # L2 归一化 → 余弦 = 点积
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = vectors / norms

    OUT_NPZ.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT_NPZ, vectors=vectors)
    OUT_JSON.write_text(json.dumps(law_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"索引已保存：{OUT_NPZ.name}（{vectors.shape}）+ {OUT_JSON.name}")
    for m in law_meta[:3]:
        logger.info(f"  样例[{m['short']}]: {m['desc'][:80]}")


if __name__ == "__main__":
    main()
