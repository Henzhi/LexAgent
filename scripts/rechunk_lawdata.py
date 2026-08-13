"""存量文档重切分：按新切分规则重新入库 LawData 中的全部存档文档。

用途：切分规则升级（如按条文边界/差异化切分）后，存量向量块仍是旧规则
产出的。本脚本从 LawData 的 txt 存档重新读取标题+正文，走
IngestionPipeline.ingest_text(force=True) 按新规则重新切分并入库，
使存量文档享受新切分效果（无需重新联网下载）。

用法:
    uv run python scripts/rechunk_lawdata.py                # 全部存档
    uv run python scripts/rechunk_lawdata.py --dry-run      # 只列出将处理的数量
    uv run python scripts/rechunk_lawdata.py --limit 20     # 只处理前 20 个
    uv run python scripts/rechunk_lawdata.py --doc-type law # 只重切分某类型
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LAW_DATA_DIR = PROJECT_ROOT / "LawData"


def collect_docs(doc_type_filter: str | None) -> list[tuple[Path, str, str]]:
    """遍历 LawData 存档，返回 [(文件路径, 标题, 归属目录)]。

    标题取 txt 首行；文件名为回退标题。
    """
    docs = []
    for fp in sorted(LAW_DATA_DIR.rglob("*.txt")):
        rel = fp.relative_to(LAW_DATA_DIR)
        # 跳过 manifest
        if fp.name.startswith("."):
            continue
        try:
            first_line = fp.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        except Exception:
            first_line = ""
        title = first_line or fp.stem
        # 归属目录 -> 规范 doc_type
        parent = rel.parts[0] if len(rel.parts) > 1 else ""
        from src.knowledge.doc_types import FLXZ_TO_DOC_TYPE
        subdir_map = {
            "constitution": "constitution",
            "laws": "law",
            "regulations": "regulation",
            "supervision_regulations": "supervision",
            "local_regulations": "local_regulation",
            "judicial_interpretations": "judicial_interpretation",
        }
        doc_type = subdir_map.get(parent, "")
        if not doc_type and first_line:
            # 根目录散落：按标题关键词兜底
            for kw, dt in FLXZ_TO_DOC_TYPE.items():
                if kw in first_line:
                    doc_type = dt
                    break
        if not doc_type:
            continue
        if doc_type_filter and doc_type != doc_type_filter:
            continue
        docs.append((fp, title, doc_type))
    return docs


def main() -> None:
    import os
    os.environ.setdefault("PG_ENABLED", "true")

    ap = argparse.ArgumentParser(description="存量文档按新切分规则重新入库")
    ap.add_argument("--dry-run", action="store_true", help="只统计不执行")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（0=全部）")
    ap.add_argument("--offset", type=int, default=0, help="从第 N 个开始（配合 --limit 分批续跑）")
    ap.add_argument("--doc-type", default=None, help="只重切分某类型（law/regulation/...）")
    args = ap.parse_args()

    from src.config import PG_CONN
    from src.knowledge.pgvector_store import PgvectorStore

    # 确保表存在
    store = PgvectorStore(PG_CONN)
    store.ensure_tables()

    docs = collect_docs(args.doc_type)
    if args.offset > 0:
        docs = docs[args.offset:]
    if args.limit > 0:
        docs = docs[: args.limit]

    logger.info(f"将处理 {len(docs)} 个存档文档（offset={args.offset}）")
    if args.dry_run:
        for fp, title, dt in docs[:20]:
            logger.info(f"  [dry] [{dt}] {title}")
        return

    # 与爬虫一致的 embedder 构造（EmbeddingAdapter 包装）
    from src.config import EMBED_MODEL, EMBED_BATCH_SIZE, EMBED_MAX_RETRIES
    from src.embedding.factory import create_embedding_backend
    from src.knowledge.ingestion.pipeline import IngestionPipeline
    from src.llm.adapter import EmbeddingAdapter

    embedder = EmbeddingAdapter(
        create_embedding_backend(
            None, model=EMBED_MODEL,
            batch_size=EMBED_BATCH_SIZE, max_retries=EMBED_MAX_RETRIES,
        )
    )
    pipeline = IngestionPipeline(store, embedder)
    ok = failed = 0
    total_chunks = 0
    for i, (fp, title, doc_type) in enumerate(docs, 1):
        try:
            text = fp.read_text(encoding="utf-8")
            n = pipeline.ingest_text(
                title=title, text=text, doc_type=doc_type,
                source="flk.npc.gov.cn", force=True, status="active",
            )
            total_chunks += n
            ok += 1
            if i % 50 == 0 or ok == 1:
                logger.info(f"[{i}/{len(docs)}] {title} → {n} 块")
        except Exception as e:
            failed += 1
            logger.warning(f"[{i}/{len(docs)}] 失败 {title}: {e}")

    logger.info(f"完成: 成功 {ok}, 失败 {failed}, 共写入 {total_chunks} 块")
    logger.info("提示: HNSW 索引增量生效，无需重建；如需整理可手动 store.reindex()")


if __name__ == "__main__":
    main()
