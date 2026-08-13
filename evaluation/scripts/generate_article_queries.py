"""从知识库实际条文生成法条级检索测试集。

策略: 从每部法律随机抽样若干条文，为每条构造两类查询:
  1. 法名+条号:  "民法典第九百六十八条"（测条款精确定位）
  2. 法名+关键词: "民法典关于合伙人出资的规定"（测关键词语义检索，关键词
     从条文正文用 jieba 提取高区分度实词）

输出: data/eval_queries_articles.json (JSONL, {id, query, relevant:[{law_name, article_number}]})

用法:
  uv run python scripts/generate_article_queries.py --per-law 3 --max-laws 60 --seed 42
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
logger = logging.getLogger("gen_article_queries")

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
OUT = EVAL_DIR / "data" / "eval_queries_articles.json"

_ARTICLE_RE = re.compile(r"^第([零一二两三四五六七八九十百千]+)条")
# 去掉条款号前缀 + 法律条文常见啰嗦词
_STRIP = re.compile(r"[第条条款款\n\r\s【】（）()、，。；：！？“”\"'《》\-—…]+")

# 法律条文高频泛词（无区分度，不应作为检索关键词）
_FUNCTION_WORDS = {
    "应当", "可以", "不得", "必须", "按照", "依照", "根据", "规定", "有关",
    "或者", "以及", "日起", "之日起", "本法", "本条例", "本规定", "本细则",
    "第一款", "第二款", "情节", "严重", "依法", "予以", "采取", "进行",
    "有关", "以下", "下列", "情形", "情况", "要求", "实施", "管理",
    "人员", "单位", "部门", "机构", "机关", "人民政府", "国家", "社会",
    "公民", "其他", "任何", "违反", "行政", "法律", "法规", "规章",
    "规定", "申请", "处理", "处罚", "责任", "义务", "权利", "工作",
    "事项", "内容", "办法", "制度", "监督", "检查", "审查", "批准",
    "报送", "提交", "出具", "负责", "主管", "审核", "受理", "公布",
    "之日起", "届满", "逾期", "数额", "标准", "条件", "程序", "范围",
    "数额", "期限", "时间", "地点", "方式", "费用", "价款", "金额",
}

# 明显的非法律术语词
_NOISE_PATTERNS = [
    re.compile(r"^[一二三四五六七八九十百千万]+$"),  # 纯数字
    re.compile(r"^.{1}$"),  # 单字
]


def int_to_cn(n: int) -> str:
    """整数 → 中文数字（仅用于生成查询文本，测试集可读性）。"""
    digits = "零一二三四五六七八九"
    units = ["", "十", "百", "千"]
    if n < 10:
        return digits[n]
    parts = []
    u = 0
    while n > 0:
        d = n % 10
        if d:
            parts.append(digits[d] + units[u])
        elif parts and not parts[-1].endswith(units[u]):
            parts.append(digits[0])
        n //= 10
        u += 1
    return "".join(reversed(parts)).replace("一十", "十")


def extract_keywords(content: str, top_k: int = 2) -> list[str]:
    """从条文正文提取高区分度关键词。

    策略: jieba 分词 → 过滤法律泛词/纯数字/单字 → 按"词长优先 + 词频次之"
    排序（法律术语通常长且有区分度，如"居住权""出资义务""追诉时效"）。
    """
    import jieba

    # 去掉"第X条"前缀
    body = _ARTICLE_RE.sub("", content or "")
    body = _STRIP.sub("", body)
    if not body:
        return []
    words = []
    for w in jieba.cut(body):
        w = w.strip()
        if len(w) < 2 or w.isdigit() or w in _FUNCTION_WORDS:
            continue
        if any(p.search(w) for p in _NOISE_PATTERNS):
            continue
        # 滤掉以泛词开头/结尾的组合噪声（如"应当加强""的规定的"）
        if w.startswith(("应当", "可以", "不得", "按照", "依照", "根据")) or \
           w.endswith(("规定", "要求", "情形", "事项", "行为")):
            continue
        words.append(w)
    if not words:
        return []
    # 词长优先（术语），同长按频次
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-len(kv[0]), -kv[1]))
    return [w for w, _ in ranked[:top_k]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-law", type=int, default=3, help="每部法律抽样条文数")
    ap.add_argument("--max-laws", type=int, default=60, help="最多覆盖法律数")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from src.config import PG_CONN
    from src.knowledge.pgvector_store import PgvectorStore

    store = PgvectorStore(PG_CONN)
    store.ensure_tables()

    # 拉取所有条文（law_name, article_range, content），按法律分组
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
        m = re.match(r"第([零一二两三四五六七八九十百千]+)条", article_range or "")
        if not m or not content:
            continue
        by_law.setdefault(law_name, []).append({
            "article_number": m.group(1),  # 中文数字，与 eval_queries.json 格式一致
            "content": content,
        })
    logger.info(f"覆盖 {len(by_law)} 部法律")

    rng = random.Random(args.seed)
    laws = rng.sample(list(by_law.items()), min(args.max_laws, len(by_law)))

    records = []
    for law_name, articles in laws:
        sample = rng.sample(articles, min(args.per_law, len(articles)))
        for a in sample:
            keywords = extract_keywords(a["content"])
            # 查询1: 法名+条号
            records.append({
                "id": f"gen-{law_name[:6]}-{a['article_number']}",
                "query": f"{law_name}第{a['article_number']}条",
                "relevant": [{"law_name": law_name, "article_number": a["article_number"]}],
            })
            # 查询2: 法名+关键词（仅当关键词足够具体才生成，避免"关于应当的规定"这类噪声）
            meaningful = [k for k in keywords if len(k) >= 3]
            if meaningful:
                kw = "关于" + "".join(meaningful[:2]) + "的规定"
                records.append({
                    "id": f"gen-{law_name[:6]}-{a['article_number']}-kw",
                    "query": f"{law_name}{kw}",
                    "relevant": [{"law_name": law_name, "article_number": a["article_number"]}],
                })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info(f"生成 {len(records)} 条查询 → {OUT}")
    # 打印样例
    for r in records[:6]:
        logger.info(f"  {r['query']} → {r['relevant']}")


if __name__ == "__main__":
    main()
