"""生成广覆盖 + 口语化的检索评测集。

为什么需要（2026-08-29 评测集审计结论，见 docs/向量路质量排查-2026-08-29.md §7）：
  - 旧语义集 eval_queries.json 只覆盖 11 部法律（库里 991 部），泛化性未验证
  - 旧法条级集 eval_queries_articles.json 是脚本生成的书面语
    （"中华人民共和国X法第Y条"），与真实用户问法差异大，其高分可能高估
    真实场景能力

本生成器的改进:
  1. **分层扩覆盖**: 按条文数量把法律分成大/中/小三层分别采样，既含常用
     大法也含冷门小法，避免"只在 11 部常用法上测"的盲区
  2. **口语化查询**: 用简称法名 + 口语模板（"劳动合同法里试用期是怎么规定的"），
     贴近真实提问；部分不带法名，模拟"用户不知道是哪部法"的场景
  3. **查询类型可区分**: id 后缀标记 precise（法名+条号）/ colloq（口语化），
     便于按类型分析指标

⚠️ 口语模板刻意保守：真实口语改写（"公司辞退我要赔偿吗"）需要语义理解，
规则模板做不到——早期版本尝试"具体怎么算""什么情况下才能XX"等模板，
会生成"有碍具体怎么算""什么情况下才能矿种"这类病句，反而污染评测集。
此处只保留对任意名词都成立的最通用模板，宁可少生成也不产出病句。
真正自然的口语查询需要 LLM 生成（见文档 §7.5 建议）。

标注: 查询由条文反向生成，gold 条文确定可靠（不引入人工标注误差）。
注意仍是**单标注**（每条只认一条正确条文）——这会系统性低估绝对指标，
但各配置横向比较时偏差是共同的，不影响相对结论。

用法:
  uv run python evaluation/scripts/generate_broad_queries.py --per-law 2 --seed 42
  uv run python evaluation/scripts/eval_retrieval.py --queries evaluation/data/eval_queries_broad.json
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("gen_broad_queries")

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR / "scripts"))
OUT = EVAL_DIR / "data" / "eval_queries_broad.json"

# 复用旧生成器的法律泛词表与噪声模式
from generate_article_queries import (  # noqa: E402
    _FUNCTION_WORDS,
    _NOISE_PATTERNS,
)

_ARTICLE_CD = re.compile(r"第([零一二两三四五六七八九十百千]+)条")

# 条文内容特征 → 决定口语模板的问法
_PENALTY_RE = re.compile(r"罚款|拘留|有期徒刑|拘役|罚金|处罚|追究刑事责任|处分")
_DEADLINE_RE = re.compile(r"期限|日内|之日起|有效期|届满|年内|个月内|日前")
_CONDITION_RE = re.compile(r"条件|符合|应当具备|经.*批准|依法.*登记")

# 金额/日期/序数量词（"四千元""三个月"）：DF 极低但无检索语义，必须排除
_NUMISH_RE = re.compile(r"^[零一二两三四五六七八九十百千万亿0-9]+[元万年个月日次项款件种倍]?$")

# 只保留名词性词性：法律术语几乎都是名词/专名/动名词；动词（"找到""进行"）
# 与数词（"四千元"）DF 往往很低，会污染"DF 越低越好"的排序
_KEEP_POS = {"n", "nr", "ns", "nt", "nz", "vn", "l", "j", "ng", "tg", "an"}


def short_name(law_name: str) -> str:
    """法名简称化：去版本后缀 + 去"中华人民共和国"前缀。

    "中华人民共和国劳动合同法(2012修正)" → "劳动合同法"
    "工伤保险条例" → "工伤保险条例"（本身已短，不动）
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", law_name or "").strip()
    s = re.sub(r"^中华人民共和国", "", s)
    return s or law_name


def build_law_df(by_law: dict, min_len: int = 2) -> dict:
    """统计每个词出现在多少部法律中（law-level 文档频率）。

    用途：挑关键词时取 DF 最低的——DF 低意味着"这个词只在少数法律里出现"，
    区分度高。"留置"只出现在监察法，"治安管理"到处都是，前者才是好关键词。

    旧生成器按"词长优先"选词，会选出"治安管理""社会治安"这类泛词，
    生成的查询既不像真人提问、也测不出检索能力，故改为 DF 优先。
    """
    import jieba
    from collections import Counter

    df: Counter = Counter()
    for articles in by_law.values():
        words = set()
        for a in articles:
            for w in jieba.cut(a["content"] or ""):
                if len(w) >= min_len and not w.isdigit():
                    words.add(w)
        df.update(words)
    return df


def pick_keywords(content: str, law_df: dict, law_name: str, top_k: int = 3,
                  max_law_df: int = 20, min_tf: int = 2) -> list[str]:
    """从条文正文挑选最具区分度的关键词：law DF 升序优先，同 DF 取长词。

    max_law_df: 关键词最多允许出现在多少部法律。超出的词视为泛词——
    不足以定位到具体条文，宁可不生成口语查询。

    min_tf: 关键词在本条文内至少出现的次数。DF 低不等于能代表条文——
    "外貌""和睦""配线"这类词 DF 极低，却只是偶然出现一次的无关词，
    据此生成的查询（"外貌有什么规定"）毫无意义。要求 TF>=2 可滤掉它们：
    一条条文反复提到的词，才是它的核心概念。
    """
    import jieba
    import jieba.posseg as pseg

    law_words = {w for w in jieba.cut(law_name or "")}
    cands = []
    for w, flag in pseg.cut(content or ""):
        w = w.strip()
        # 泛词、单字、纯数字、法名里已有的词都不要（法名词无法定位到具体条文）
        if len(w) < 2 or w.isdigit() or w in _FUNCTION_WORDS or w in law_words:
            continue
        if any(p.search(w) for p in _NOISE_PATTERNS):
            continue
        if _NUMISH_RE.match(w):      # 金额/日期/序数量词
            continue
        if flag not in _KEEP_POS:    # 只保留名词性（动词"找到"等会被排除）
            continue
        cands.append(w)
    if not cands:
        return []
    seen: dict[str, int] = {}
    for w in cands:
        seen[w] = seen.get(w, 0) + 1
    # DF 升序（越稀有越靠前）→ 同 DF 时词长降序（术语优先）
    ranked = sorted(seen.items(), key=lambda kv: (law_df.get(kv[0], 999), -len(kv[0])))
    return [
        w for w, tf in ranked[:top_k]
        if law_df.get(w, 999) <= max_law_df and tf >= min_tf
    ]


def build_colloquial(short: str, keywords: list[str], content: str, rng: random.Random) -> str | None:
    """按条文内容特征选口语模板，生成贴近真实提问的查询。

    模板保守：只保留对任意名词都成立的通用问法（见模块 docstring）。
    """
    if not keywords:
        return None
    kw = keywords[0]
    if len(kw) < 2:
        return None

    if _PENALTY_RE.search(content or ""):
        tpl = rng.choice([
            "{kw}的法律后果是什么",
            "违反{kw}要承担什么责任",
        ])
    elif _DEADLINE_RE.search(content or ""):
        tpl = rng.choice([
            "{kw}的期限是多久",
            "{kw}有没有时间限制",
        ])
    elif _CONDITION_RE.search(content or ""):
        tpl = rng.choice([
            "{kw}需要满足什么条件",
            "{kw}有什么要求",
        ])
    else:
        tpl = rng.choice([
            "{kw}是怎么规定的",
            "{kw}有什么规定",
        ])
    q = tpl.format(kw=kw)

    # 70% 带法名（用户常知道大概是哪部法），30% 不带（模拟不知道法名的场景）
    if rng.random() < 0.7:
        q = rng.choice([f"{short}里{q}", f"请问{short}的{q}"])
    return q + "？"


def layered_sample(laws: dict, rng: random.Random, big=25, mid=45, small=70) -> list:
    """按条文数量分大/中/小三层采样，保证覆盖广度。

    纯随机采样会让冷门小法占多数（小法数量远多于大法），而真实查询
    集中在大法上；分层能同时覆盖两端。
    """
    items = sorted(laws.items(), key=lambda kv: -len(kv[1]))
    n = len(items)
    bands = [
        (0, min(big, n)),                                  # 大法（条文最多，常用）
        (min(big, n), min(big + mid, n)),                  # 中法
        (min(big + mid, n), min(big + mid + small, n)),    # 小法（冷门）
    ]
    picked = []
    for lo, hi in bands:
        band = items[lo:hi]
        if not band:
            continue
        picked.extend(rng.sample(band, min(len(band), hi - lo)))
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-law", type=int, default=2, help="每部法律抽样条文数")
    ap.add_argument("--big", type=int, default=25, help="大法（条文最多）采样部数")
    ap.add_argument("--mid", type=int, default=45, help="中法采样部数")
    ap.add_argument("--small", type=int, default=70, help="小法（冷门）采样部数")
    ap.add_argument("--min-content", type=int, default=30, help="条文正文最短长度（过短多为碎片）")
    ap.add_argument("--max-law-df", type=int, default=20,
                    help="关键词最多允许出现在多少部法律（越小越独特，超阈值则不生成口语查询）")
    ap.add_argument("--min-tf", type=int, default=2,
                    help="关键词在本条文内至少出现的次数（滤掉偶然出现的无关词）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from src.config import PG_CONN
    from src.knowledge.pgvector_store import PgvectorStore

    store = PgvectorStore(PG_CONN)
    store.ensure_tables()

    with store._conn.cursor() as cur:
        cur.execute(
            "SELECT dc.metadata->>'law_name', dc.metadata->>'article_range', dc.content "
            "FROM document_chunks dc "
            "JOIN documents d ON dc.doc_id = d.id "
            "WHERE d.status='active' AND dc.chunk_type='article'"
        )
        rows = cur.fetchall()
    logger.info(f"共读取 {len(rows)} 条条文")

    by_law: dict[str, list] = {}
    for law_name, article_range, content in rows:
        m = _ARTICLE_CD.match(article_range or "")
        # 过短的正文多为切分碎片（排查发现占 6.4%），不用来生成查询
        if not m or not content or len(content) < args.min_content:
            continue
        by_law.setdefault(law_name, []).append({
            "article_number": m.group(1),
            "content": content,
        })
    logger.info(f"覆盖 {len(by_law)} 部法律（已过滤碎片）")

    law_df = build_law_df(by_law)
    logger.info(f"law-level 词频统计完成（{len(law_df)} 个词）")

    rng = random.Random(args.seed)
    picked = layered_sample(by_law, rng, big=args.big, mid=args.mid, small=args.small)
    logger.info(f"分层采样 {len(picked)} 部法律")

    records = []
    skipped = 0
    for law_name, articles in picked:
        short = short_name(law_name)
        sample = rng.sample(articles, min(args.per_law, len(articles)))
        for a in sample:
            base = {
                "relevant": [{"law_name": law_name, "article_number": a["article_number"]}],
            }
            # 类型1: 法名+条号（精确型，测 Router / BM25）
            records.append({
                **base,
                "id": f"b-{short[:6]}-{a['article_number']}-precise",
                "query": f"{law_name}第{a['article_number']}条",
            })
            # 类型2: 口语化（真实问法，测语义检索）；关键词不够独特则跳过
            keywords = pick_keywords(a["content"], law_df, law_name,
                                     max_law_df=args.max_law_df,
                                     min_tf=args.min_tf)
            cq = build_colloquial(short, keywords, a["content"], rng)
            if cq:
                records.append({
                    **base,
                    "id": f"b-{short[:6]}-{a['article_number']}-colloq",
                    "query": cq,
                })
            else:
                skipped += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    laws_covered = len({r["relevant"][0]["law_name"] for r in records})
    logger.info(
        f"生成 {len(records)} 条查询（覆盖 {laws_covered} 部法律，"
        f"{skipped} 条因关键词不够独特跳过）→ {OUT}"
    )
    for r in records[:8]:
        logger.info(f"  [{r['id'].split('-')[-1]:<8}] {r['query']}")


if __name__ == "__main__":
    main()
