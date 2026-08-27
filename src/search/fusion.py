"""
双路检索结果融合与冲突裁决（M2 / F6-F8，决策 D-M2-2 / D-M2-5）。

输入三路证据（ReAct 循环中 tools_node 累计到 state）：
- internal_docs：内部 pgvector 知识库检索结果（最高优先级法律依据，REQ-U3）；
- web_results：Tavily 网络搜索结果（仅作线索，不得直接作为最终法律依据）；
- legal_results：官方法律源结果（国家法律法规数据库 / 案例库 / 小包公）。

输出统一 sources 列表（供 SSE meta 与引用溯源 F10）：
- 按 来源权重 × 相关度 排序：内部库 > 官方源 > 网络；
- 跨来源去重（内部按 法名+条号；官方/网络按 URL）；
- 每条携带 verification 验证状态：
    verified_internal  内部库（经校验的权威数据）
    verified_official  官方源已验证（国家法律法规数据库 / 官方案例库域）
    third_party        第三方源（小包公）
    web_unverified     网络线索（未验证）
- 冲突裁决（REQ-UW3）：web 结果提及内部库已收录法名 → 该 web 条目标记
  superseded=True（内部库优先），并在返回值 notes 中汇总冲突法名，
  供上层提示用户"该信息以内部库为准/未经官方验证"。
"""
from __future__ import annotations

import re
from typing import Any

from src.config import (
    FUSION_TOP_K,
    FUSION_WEIGHT_INTERNAL,
    FUSION_WEIGHT_LEGAL,
    FUSION_WEIGHT_WEB,
)
from src.search.legal_sources import (
    SOURCE_COURT_CASE_LIB,
    SOURCE_NATIONAL_LAW_DB,
    SOURCE_XBG,
)

# 验证状态常量（SSE meta.sources.verification / F10 引用溯源）
VERIFIED_INTERNAL = "verified_internal"
VERIFIED_OFFICIAL = "verified_official"
THIRD_PARTY = "third_party"
WEB_UNVERIFIED = "web_unverified"

# 官方源子来源 → 验证状态映射
_LEGAL_SUBSOURCE_VERIFICATION = {
    SOURCE_NATIONAL_LAW_DB: VERIFIED_OFFICIAL,
    SOURCE_COURT_CASE_LIB: VERIFIED_OFFICIAL,
    SOURCE_XBG: THIRD_PARTY,
}


def _norm_law_name(name: str) -> str:
    """法名归一化：去掉书名号与空白（内部库 law_name 常带《》，文本提取的不带）。"""
    for ch in "《〈》〉「」『』":
        name = name.replace(ch, "")
    return name.strip()


def _extract_law_names(text: str) -> set[str]:
    """从文本中提取《XX法》形式的法名并归一化（冲突检测用，宽松匹配）。"""
    if not text:
        return set()
    return {
        _norm_law_name(m)
        for m in re.findall(r"[《〈]([^《》〉]{2,30})[》〉]", str(text))
        if m.strip()
    }


def _internal_key(doc: dict) -> tuple:
    """内部库条目去重键：法名 + 条号。"""
    return (doc.get("law_name") or "", doc.get("article_range") or "")


def fuse_evidence(
    internal_docs: list[dict],
    web_results: list[dict],
    legal_results: list[dict],
    top_k: int = FUSION_TOP_K,
) -> dict[str, Any]:
    """三路证据融合、去重、排序与冲突裁决（F6/F7/F8）。

    Args:
        internal_docs: 内部库条目（retrieve_knowledge 的 docs 字典）
        web_results: 网络搜索条目（web_search 的 results 字典，Tavily 结构）
        legal_results: 官方源条目（legal_source_search 的 results 字典）
        top_k: 输出条数上限

    Returns:
        {"sources": [统一来源条目], "count": 条数,
         "conflict_laws": [冲突法名], "web_conflicts": [冲突的 web 条目数]}
    """
    items: list[dict[str, Any]] = []
    seen_internal: set[tuple] = set()
    seen_url: set[str] = set()

    # ---- 1. 内部库（权重最高，先入列表）----
    internal_law_names: set[str] = set()
    for d in internal_docs or []:
        key = _internal_key(d)
        if key in seen_internal:
            continue
        seen_internal.add(key)
        law_name = (d.get("law_name") or "").strip()
        if law_name:
            internal_law_names.add(_norm_law_name(law_name))
        items.append({
            "law_name": law_name,
            "chapter": d.get("chapter", ""),
            "section": d.get("section", ""),
            "article_range": d.get("article_range", ""),
            "citation": d.get("citation", ""),
            "content": d.get("content", ""),
            "score": float(d.get("score", 0.0) or 0.0),
            "source": "internal_kb",
            "verification": VERIFIED_INTERNAL,
            "fused_score": FUSION_WEIGHT_INTERNAL * (0.5 + 0.5 * float(d.get("score", 0.0) or 0.0)),
        })

    # ---- 2. 官方源（验证状态按子来源）----
    for r in legal_results or []:
        url = (r.get("url") or "").strip()
        if url:
            if url in seen_url:
                continue
            seen_url.add(url)
        sub = r.get("source") or SOURCE_NATIONAL_LAW_DB
        items.append({
            "law_name": (r.get("title") or "").strip(),
            "chapter": "",
            "section": "",
            "article_range": "",
            "citation": (r.get("title") or "").strip(),
            "content": r.get("content", ""),
            "score": float(r.get("score", 0.0) or 0.0),
            "source": "legal_source",
            "sub_source": sub,
            "verification": _LEGAL_SUBSOURCE_VERIFICATION.get(sub, VERIFIED_OFFICIAL),
            "law_status": r.get("law_status", ""),
            "url": url,
            "fused_score": FUSION_WEIGHT_LEGAL,
        })

    # ---- 3. 网络结果（仅线索；与内部库法名重合 → 冲突标记，内部库优先）----
    conflict_laws: set[str] = set()
    web_conflicts = 0
    for r in web_results or []:
        url = (r.get("url") or "").strip()
        if url:
            if url in seen_url:
                continue
            seen_url.add(url)
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        # 冲突裁决（D-M2-5）：web 文本提及内部库已收录法名
        mentioned = _extract_law_names(f"{title} {content}") & internal_law_names
        superseded = bool(mentioned)
        if superseded:
            web_conflicts += 1
            conflict_laws.update(mentioned)
        tavily_score = max(0.0, min(float(r.get("score", 0.0) or 0.0), 1.0))
        items.append({
            "law_name": title,
            "chapter": "",
            "section": "",
            "article_range": "",
            "citation": title,
            "content": content,
            "score": tavily_score,
            "source": "web",
            "verification": WEB_UNVERIFIED,
            "url": url,
            "superseded": superseded,          # True = 内部库已有该法，以内部库为准
            "fused_score": FUSION_WEIGHT_WEB * tavily_score,
        })

    # ---- 4. 排序（fused_score 降序；内部库天然权重最高）+ 截断 ----
    items.sort(key=lambda x: x["fused_score"], reverse=True)
    k = max(1, int(top_k))
    sources = items[:k]

    return {
        "sources": sources,
        "count": len(sources),
        "conflict_laws": sorted(conflict_laws),
        "web_conflicts": web_conflicts,
    }
