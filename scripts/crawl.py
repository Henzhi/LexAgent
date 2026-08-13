"""法律爬虫命令行入口（国家法律法规数据库，增量更新）。

用法:
    uv run python scripts/crawl.py --doc-type law --keyword 刑法 --limit 10
    uv run python scripts/crawl.py --doc-type auto --keyword 数据安全法   # 按 flxz 自动分类
    uv run python scripts/crawl.py --doc-type all --limit 0        # 全量(不限条数)
    uv run python scripts/crawl.py --doc-type law --force          # 强制重爬
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge.crawler import NpcLawCrawler, TYPE_MAP


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Law-RAG-Agent 法律爬虫（国家法律法规数据库，增量更新）"
    )
    ap.add_argument("--source", default="npc", help="数据源，目前仅支持 npc")
    ap.add_argument(
        "--doc-type", default="law",
        choices=list(TYPE_MAP.keys()) + ["auto", "all"],
        help="文档类型: " + "/".join(TYPE_MAP.keys()) + " / auto(按flxz自动分类) / all",
    )
    ap.add_argument("--keyword", default="", help="标题模糊搜索关键词（空=该类型全部）")
    ap.add_argument("--limit", type=int, default=50, help="最多爬取条数，0=不限")
    ap.add_argument("--force", action="store_true", help="强制重爬已存在的文档")
    ap.add_argument("--subdir", default="", help="覆盖输出子目录名（默认按 doc_type 自动）")
    ap.add_argument(
        "--store", default="pg",
        help="输出目标: pg(pgvector，推荐) / txt(LawData 原始文本存档) / both；可组合如 pg,txt",
    )
    ap.add_argument(
        "--sxx", default="3", dest="sxx",
        help="效力状态过滤（flk 码，可逗号组合）：1=已废止 2=已修改 3=现行有效 4=尚未生效；默认 3（仅现行有效）",
    )
    ap.add_argument("--sleep", type=float, default=1.0, help="请求间隔秒数（默认 1.0，限流用）")
    ap.add_argument("--quiet", action="store_true", help="仅输出统计结果")
    args = ap.parse_args()

    if args.source != "npc":
        print("暂仅支持 source=npc（国家法律法规数据库）")
        sys.exit(2)

    crawler = NpcLawCrawler(sleep=args.sleep)
    sxx = [s.strip() for s in args.sxx.split(",") if s.strip()]
    res = crawler.crawl(
        doc_type=args.doc_type, keyword=args.keyword,
        limit=args.limit, force=args.force, subdir=args.subdir,
        store=args.store, sxx=sxx,
    )

    if not args.quiet:
        print(
            f"\n爬取完成: 命中 {res.total} | 新增 {res.added} | "
            f"更新 {res.updated} | 跳过 {res.skipped} | 失败 {res.failed}"
        )
        if res.errors:
            print("失败项:")
            for e in res.errors[:20]:
                print(f"  - {e}")
        print(f"新增/更新文件数: {len(res.files)}")
        if res.added or res.updated:
            if "pg" in (args.store or "pg").lower():
                print("已写入 pgvector，可直接检索。")
            else:
                print("提示: 仅 txt 存档未入库，请使用 store=pg 或 both 写入 pgvector")
    else:
        print(f"added={res.added} updated={res.updated} skipped={res.skipped} "
              f"failed={res.failed} total={res.total}")


if __name__ == "__main__":
    main()
