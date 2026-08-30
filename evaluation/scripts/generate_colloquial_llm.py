"""用 LLM 生成真实口语化查询（B1，补 generate_broad_queries 规则模板做不到的部分）。

背景（docs/向量路质量排查-2026-08-29.md §7 + 8/30 评测结论）：
  - 规则模板的口语化仍是"名词填空"（"劳动合同法里试用期是怎么规定的"），
    且生成器跳过了 197 条候选；真实口语（"公司辞退我要赔偿吗"）需要语义
    理解，是 LLM 的活。
  - 最要紧的缺口是**不带法名**的问法：现有口语集 57.4% 带法名 vs 不带
    仅 6.7% 命中——本生成器产出的查询默认禁止出现法名与条号（模拟
    "用户不知道该问哪部法"的最难场景）。

标注可靠性：查询由条文反向生成，gold 条文 by construction 可靠；
生成后做硬校验（含法名/条号/超长即丢弃，重试一次，再失败跳过）——
宁可少生成也不污染评测集（8/29 生成器三轮调优的教训）。

用法:
  uv run python evaluation/scripts/generate_colloquial_llm.py --limit 5   # 冒烟
  uv run python evaluation/scripts/generate_colloquial_llm.py             # 全量（真实 DeepSeek API）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("gen_colloq_llm")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR / "scripts"))
sys.path.insert(0, str(ROOT))

OUT = EVAL_DIR / "data" / "eval_queries_colloq_llm.json"
_EXISTING = EVAL_DIR / "data" / "eval_queries_broad.json"

_ARTICLE_CD = re.compile(r"第([零一二两三四五六七八九十百千]+)条")
_BAD_RE = re.compile(r"第[零一二两三四五六七八九十百千\d]+条|《.+?》|根据|依据|的规定|法律条文")

_GEN_SYSTEM = "你是一位普通的中国法律用户。只输出问题本身，不要任何解释、引号或标点以外的内容。"
_GEN_TMPL = """根据下面的法律条文，写一个普通人会问的口语化问题。

硬性要求：
1. 问题里绝对不能出现法律名称（如"{short}"），也不能出现"第X条"、《》、"根据"等书面痕迹
2. 自然口语，像"公司辞退我要赔偿吗"这样的问法，不要书面语
3. 问题必须能用这条条文**直接回答**，围绕它的核心概念：{keywords}
4. 长度 8~40 个字

法条内容：{content}

只输出这一个口语问题："""


def _mk_llm():
    from src.llm.factory import create_llm_backend

    return create_llm_backend(
        backend_type="openai",
        model="deepseek-v4-flash",
        api_key=os.getenv("OPENAI_API_KEY", ""),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
        max_tokens=128,
        temperature=0.7,
    )


def short_name(law_name: str) -> str:
    s = re.sub(r"[（(][^）)]*[）)]", "", law_name or "").strip()
    s = re.sub(r"^中华人民共和国", "", s)
    return s or law_name


def _used_pairs() -> set[tuple[str, str]]:
    """已有 broad 集里用过的（法名, 条号），避免与规则生成集重复采样。"""
    used: set[tuple[str, str]] = set()
    if _EXISTING.exists():
        for line in _EXISTING.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            for rel in r.get("relevant", []):
                used.add((rel.get("law_name", ""), str(rel.get("article_number", ""))))
    return used


def validate(q: str, law_name: str, short: str) -> bool:
    """硬校验：含法名/条号/书面痕迹、长度出界即不合格。"""
    q = q.strip().strip('"「」“”')
    if not (8 <= len(q) <= 40):
        return False
    if _BAD_RE.search(q):
        return False
    # 法名（全称或简称）整体出现即不合格
    for name in {law_name, short}:
        if name and name in q:
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description="LLM 生成无 法名口语查询")
    ap.add_argument("--per-law", type=int, default=1, help="每部法律抽几条条文")
    ap.add_argument("--big", type=int, default=25)
    ap.add_argument("--mid", type=int, default=45)
    ap.add_argument("--small", type=int, default=80)
    ap.add_argument("--min-content", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="最多生成 N 条（冒烟用，0=不限制）")
    ap.add_argument("--seed", type=int, default=20260830)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from src.config import PG_CONN
    from src.knowledge.pgvector_store import PgvectorStore
    from generate_broad_queries import build_law_df, layered_sample, pick_keywords  # noqa: E402

    store = PgvectorStore(PG_CONN)
    store.ensure_tables()
    with store._conn.cursor() as cur:
        cur.execute(
            "SELECT dc.metadata->>'law_name', dc.metadata->>'article_range', dc.content "
            "FROM document_chunks dc JOIN documents d ON dc.doc_id = d.id "
            "WHERE d.status='active' AND dc.chunk_type='article'"
        )
        rows = cur.fetchall()
    by_law: dict[str, list] = {}
    for law_name, article_range, content in rows:
        m = _ARTICLE_CD.match(article_range or "")
        if not m or not content or len(content) < args.min_content:
            continue
        by_law.setdefault(law_name, []).append({"article_number": m.group(1), "content": content})
    logger.info(f"条文池 {len(by_law)} 部法律")

    import random

    rng = random.Random(args.seed)
    used = _used_pairs()
    picked = layered_sample(by_law, rng, big=args.big, mid=args.mid, small=args.small)
    law_df = build_law_df(by_law)

    llm = _mk_llm()
    records, skipped = [], 0
    for law_name, articles in picked:
        short = short_name(law_name)
        sample = rng.sample(articles, min(args.per_law, len(articles)))
        for a in sample:
            if args.limit and len(records) >= args.limit:
                break
            if (law_name, a["article_number"]) in used:
                continue  # 已有同条文查询，不重复采样
            kws = pick_keywords(a["content"], law_df, law_name, max_law_df=25, min_tf=2)
            kw_hint = "、".join(kws) if kws else "该条文的核心规定"
            q = ""
            for _ in range(2):  # 校验不过重试一次
                try:
                    q = llm.chat(
                        _GEN_TMPL.format(short=short, keywords=kw_hint, content=a["content"][:500]),
                        system_prompt=_GEN_SYSTEM,
                    ).strip()
                except Exception as e:
                    logger.warning(f"生成失败（跳过）: {e}")
                    q = ""
                    break
                if validate(q, law_name, short):
                    break
                q = ""
            if not q:
                skipped += 1
                continue
            records.append({
                "id": f"l-{short[:6]}-{a['article_number']}-colloq",
                "query": q,
                "relevant": [{"law_name": law_name, "article_number": a["article_number"]}],
            })
            logger.info(f"[{len(records):>3}] {q}")

    OUT.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    laws = {r["relevant"][0]["law_name"] for r in records}
    logger.info(f"生成 {len(records)} 条（覆盖 {len(laws)} 部法律，{skipped} 条校验不过跳过）→ {OUT}")


if __name__ == "__main__":
    main()
