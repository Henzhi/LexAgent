"""从数据库现有 chunks 全量重建 data/vector_store/article_map.json。

背景: article_map.json 仅在入库时增量更新；存量重切分（rechunk_lawdata.py 直写 DB）
未触发更新，导致生产配置中 AdjacentExpander 空转（相邻条文扩展不生效）。

用法:
  uv run python scripts/rebuild_article_map.py

输出: {law_name: {条款号字符串: {content, article_range, chapter, section}}}
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / "data" / "vector_store" / "article_map.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("rebuild_article_map")

CN_TO_INT = {
    '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000,
}


def cn2int(cn: str) -> int:
    result, unit = 0, 1
    i = len(cn) - 1
    while i >= 0:
        val = CN_TO_INT.get(cn[i], 0)
        if val >= 10:
            unit = val
            if i == 0:
                result += unit
            i -= 1
            continue
        result += val * unit
        unit = 1
        i -= 1
    return result


def main():
    sys.path.insert(0, str(ROOT))
    from src.config import PG_CONN
    from src.knowledge.pgvector_store import PgvectorStore

    store = PgvectorStore(PG_CONN)
    store.ensure_tables()

    # 拉取所有带 article_range 的 active 条文块
    with store._conn.cursor() as cur:
        cur.execute("""
            SELECT dc.metadata, dc.content
            FROM document_chunks dc
            JOIN documents d ON dc.doc_id = d.id
            WHERE d.status = 'active'
        """)
        rows = cur.fetchall()
    logger.info(f"读取到 {len(rows)} 个块")

    article_map: dict[str, dict] = {}
    parsed = 0
    for meta_raw, content in rows:
        meta = meta_raw or {}
        law_name = meta.get("law_name", "")
        article_range = meta.get("article_range", "")
        if not law_name or not article_range:
            continue
        m = re.search(r"第([零一二两三四五六七八九十百千]+)条", article_range)
        if not m:
            continue
        num = cn2int(m.group(1))
        article_map.setdefault(law_name, {})[str(num)] = {
            "content": content,
            "article_range": article_range,
            "chapter": meta.get("chapter", ""),
            "section": meta.get("section", ""),
        }
        parsed += 1

    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(article_map, f, ensure_ascii=False, indent=2)
    tmp.replace(MAP_PATH)

    n_laws = len(article_map)
    n_articles = sum(len(v) for v in article_map.values())
    logger.info(f"重建完成: {n_laws} 部法律 / {n_articles} 条 → {MAP_PATH}")


if __name__ == "__main__":
    main()
