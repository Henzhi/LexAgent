"""将 LawData 目录下已落盘的 txt 全部入库到 pgvector（跳过已存在的文档）。

用法:
    uv run python scripts/ingest_lawdata.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.crawler import NpcLawCrawler

ROOT = PROJECT_ROOT
LAWDATA = ROOT / "LawData"

# 子目录名 -> doc_type
SUBMAP = {
    "constitution": "constitution",
    "laws": "law",
    "regulations": "regulation",
    "supervision_regulations": "supervision",
    "judicial_interpretations": "judicial",
    "local_regulations": "local",
}


def doc_type_for(path: Path) -> str:
    rel = path.relative_to(LAWDATA)
    parts = rel.parts
    if len(parts) >= 2:
        return SUBMAP.get(parts[0], "law")
    # 顶层目录：含"宪法"归 constitution，其余归 law
    return "constitution" if "宪法" in path.stem else "law"


def main() -> int:
    if not LAWDATA.exists():
        print(f"未找到 LawData 目录: {LAWDATA}")
        return 1

    crawler = NpcLawCrawler()
    crawler._ensure_pg()  # 懒加载 pg store + pipeline

    files = sorted(LAWDATA.rglob("*.txt"))
    if not files:
        print("LawData 下没有 txt 文件。")
        return 0

    total = skipped = ingested = 0
    for i, f in enumerate(files, 1):
        title = f.stem.strip()
        dt = doc_type_for(f)
        text = f.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            continue
        n = crawler._pg_pipeline.ingest_text(
            title=title,
            text=text,
            doc_type=dt,
            source="LawData",
            force=False,  # 已存在则跳过，不重复入库
        )
        total += 1
        if n == 0:
            skipped += 1
        else:
            ingested += 1
        if i % 20 == 0 or i == len(files):
            print(
                f"进度 {i}/{len(files)} | 已入库 {ingested} | 跳过(已存在) {skipped}",
                flush=True,
            )

    # 全部写入后统一重建 HNSW 索引
    crawler._pg_store.reindex()
    print(
        f"完成: 处理 {total} 个文件 | 新入库 {ingested} | 跳过(已存在) {skipped} | "
        f"HNSW 索引已重建",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
