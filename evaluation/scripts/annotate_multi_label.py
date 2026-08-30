"""为语义评测集补充多标注（B1，2026-08-30 评测集审计的改进项）。

为什么（docs/向量路质量排查-2026-08-29.md §7）：单标注系统性低估——
抽查 12 条未命中，至少 3 条是「系统比标注更准」（如「试用期交社保吗」
标注劳动法73条 vs 系统社保法58条须30日内登记）。多标注（每条 2~5 条
相关条文）后 Hit@k 才反映真实召回水平。**原标注永远保留**（只增不减），
避免把人审过的标注冲掉。

方法（LLM 辅助判定 + 人工抽审）：
1. 候选生成：向量 bare top10 + BM25 top10 取并集（含原标注条文）；
2. LLM 逐条判定：给定问题 + 候选条文（法名/条号/正文），判断是否
   「能直接作为回答该问题的法律依据」（从严：泛泛同类不算）；
3. 合并：原标注在前 + LLM 判相关（按 (法名,条号) 去重，最多 5 条）；
4. 人工抽审：随机 25 条输出 markdown 报告（evaluation/data/lexeval/
   multi_label_review_sample.md），核对 LLM 判定精度后再全量采信。

用法:
  uv run python evaluation/scripts/annotate_multi_label.py --limit 3   # 冒烟
  uv run python evaluation/scripts/annotate_multi_label.py             # 全量（真实 DeepSeek API）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("annotate_multi_label")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EVAL_DIR = Path(__file__).resolve().parent.parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR / "scripts"))
sys.path.insert(0, str(ROOT))

SRC = EVAL_DIR / "data" / "eval_queries.json"
OUT = EVAL_DIR / "data" / "eval_queries_multi.json"
REVIEW = EVAL_DIR / "data" / "lexeval" / "multi_label_review_sample.md"

_JUDGE_SYSTEM = "你是法律检索标注员。只输出 JSON，不要输出任何其他文字。"
_JUDGE_TMPL = """给定用户问题与候选法条，判断每条候选是否「能直接作为回答该问题的法律依据」。

判定标准（从严）：
- 相关 = 该条文的明文规定是回答这个问题需要的依据（可直接引用作答）；
- 仅主题相近、或只是同一部法律的其他条文 → 不相关；
- 最多 5 条相关；一条都不相关就返回空数组。

用户问题：{query}

候选法条（idx 从 0 开始）：
{candidates}

只输出 JSON：{{"relevant": [相关候选的 idx, ...]}}"""


def _mk_llm():
    """显式用 DeepSeek 主模型（脚本直连，不走 .env 的 LLM_MODEL 本地模型名）。"""
    from src.llm.factory import create_llm_backend

    return create_llm_backend(
        backend_type="openai",
        model="deepseek-v4-flash",
        api_key=os.getenv("OPENAI_API_KEY", ""),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
        max_tokens=512,
    )


def _norm_cand(doc) -> dict | None:
    """检索结果（dict 或 RetrievedDoc）→ 统一候选 dict。"""
    g = (lambda k: doc.get(k, "")) if isinstance(doc, dict) else (lambda k: getattr(doc, k, "") or "")
    law_name = (g("law_name") or "").strip()
    article_range = (g("article_range") or "").strip()
    content = (g("content") or "").strip()
    if not law_name or not article_range:
        return None
    return {"law_name": law_name, "article_range": article_range, "content": content}


def _norm_law(name: str) -> str:
    """法名归一化（去版本后缀 + 去"中华人民共和国"前缀）——原标注常带
    版本后缀（"(2025修订)"）而检索返回不带，直接比较会导致去重失效。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", name or "")
    return re.sub(r"^中华人民共和国", "", s).strip()


def _cand_key(c: dict) -> tuple[str, str]:
    """按（归一化法名, 首个条号）去重。"""
    m = re.search(r"第[零一二两三四五六七八九十百千\d]+条", c["article_range"])
    return (_norm_law(c["law_name"]), m.group(0) if m else c["article_range"])


def gather_candidates(query: str, retriever, bm25, top_k: int = 10) -> list[dict]:
    """向量 bare top-k + BM25 top-k 取并集（按候选键去重，保持向量路顺序优先）。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for doc in retriever.search(query, top_k=top_k):
        c = _norm_cand(doc)
        if c and _cand_key(c) not in seen:
            seen.add(_cand_key(c))
            out.append(c)
    if bm25 is not None:
        for r in bm25.search(query, top_k=top_k):
            c = _norm_cand(r)
            if c and _cand_key(c) not in seen:
                seen.add(_cand_key(c))
                out.append(c)
    return out


def judge(llm, query: str, candidates: list[dict]) -> list[int]:
    """LLM 判定相关候选 idx 列表；解析失败返回 []（调用方保留原标注）。"""
    lines = []
    for i, c in enumerate(candidates):
        lines.append(f"[{i}] {c['law_name']} {c['article_range']}\n{c['content'][:280]}")
    prompt = _JUDGE_TMPL.format(query=query, candidates="\n".join(lines))
    try:
        resp = llm.chat(prompt, system_prompt=_JUDGE_SYSTEM)
    except Exception as e:
        logger.warning(f"LLM 判定失败（保留原标注）: {e}")
        return []
    m = re.search(r"\{[^{}]*\}", resp, re.DOTALL)
    if not m:
        logger.warning(f"LLM 输出非 JSON，跳过: {resp[:80]}")
        return []
    try:
        idxs = json.loads(m.group(0)).get("relevant", [])
        return [int(i) for i in idxs if isinstance(i, (int, float)) and 0 <= int(i) < len(candidates)]
    except (ValueError, TypeError):
        return []


def main():
    ap = argparse.ArgumentParser(description="语义集多标注（LLM 辅助 + 原标注保留）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部，冒烟用）")
    ap.add_argument("--max-labels", type=int, default=5)
    ap.add_argument("--review-n", type=int, default=25, help="人工抽审样本条数")
    ap.add_argument("--seed", type=int, default=20260830)
    args = ap.parse_args()

    queries = json.loads(SRC.read_text(encoding="utf-8"))
    if args.limit:
        queries = queries[: args.limit]
    logger.info(f"语义集 {len(queries)} 条，开始多标注（候选=向量top10+BM25 top10 并集）")

    # 候选检索器：bare 向量 + BM25（都是粗排，足够宽，不做精排——judge 只看内容）
    from eval_retrieval import build_retriever  # noqa: E402 复用评测基建

    retriever, _ = build_retriever(bare=True, mode="vector")
    try:
        bm25, _ = build_retriever(bare=True, mode="bm25")
    except Exception as e:
        logger.warning(f"BM25 不可用，仅用向量候选: {e}")
        bm25 = None
    llm = _mk_llm()

    out, n_expanded, n_judge_fail = [], 0, 0
    for i, q in enumerate(queries, 1):
        original = q.get("relevant", [])
        cands = gather_candidates(q["query"], retriever, bm25)
        idxs = judge(llm, q["query"], cands)
        merged: list[dict] = []
        seen: set[tuple[str, str]] = set()
        # 原标注永远保留在前
        for rel in original:
            key = (_norm_law(rel.get("law_name", "")), f"第{rel.get('article_number', '')}条")
            if key not in seen:
                seen.add(key)
                merged.append(dict(rel))
        for idx in idxs:
            c = cands[idx]
            num_m = re.search(r"第([零一二两三四五六七八九十百千\d]+)条", c["article_range"])
            if not num_m:
                continue
            key = (_norm_law(c["law_name"]), num_m.group(0))
            if key in seen:
                continue
            seen.add(key)
            merged.append({"law_name": c["law_name"], "article_number": num_m.group(1), "via_llm": True})
        merged = merged[: args.max_labels]
        if len(merged) > len(original):
            n_expanded += 1
        if not idxs and merged == original:
            n_judge_fail += 1
        out.append({**q, "relevant": merged})
        logger.info(
            f"[{i:>3}/{len(queries)}] {len(original)}→{len(merged)} 标注 | {q['query'][:36]}"
        )

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"完成 → {OUT}（扩充 {n_expanded} 条，判定失败/无新增 {n_judge_fail} 条保留原标注）")

    # ---- 人工抽审报告 ----
    rng = random.Random(args.seed)
    sample = rng.sample(out, min(args.review_n, len(out)))
    lines = ["# 多标注人工抽审样本", "",
             f"> 从 {len(out)} 条中随机抽 {len(sample)} 条。请核对：每条标注是否真的与问题相关",
             "> （重点看 `via_llm=true` 的新增标注有没有「看着顺眼其实不相关」的）。",
             "> 原标注（无 via_llm 标记）永远保留，不受影响。", ""]
    for j, q in enumerate(sample, 1):
        lines.append(f"## {j}. {q['query']}")
        for rel in q["relevant"]:
            tag = " 🆕LLM" if rel.get("via_llm") else "（原标注）"
            lines.append(f"- {rel['law_name']} 第{rel['article_number']}条 {tag}")
        lines.append("")
    REVIEW.parent.mkdir(parents=True, exist_ok=True)
    REVIEW.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"人工抽审样本（{len(sample)} 条）→ {REVIEW}")


if __name__ == "__main__":
    main()
