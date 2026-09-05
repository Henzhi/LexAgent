"""
审核子图（M4 阶段 2，决策 D-M4-3；docs/M4-多Agent路线图.md §4 阶段 2 第一个真子 Agent）。

升级旧 validate 节点（单次 LLM PASS 判定）为三层结构化审核，输出独立判定契约
（state.review），同时保持旧重试语义（validation_passed / validation_feedback /
retry_count）完全兼容——validate → should_retry → generate 的图结构零改动：

  L1 review_rule    规则守卫：HallucinationGuard（检索置信度 + 内容安全），纯规则零成本；
                    拦截 → 短路 final（validation_passed=True 放行，外层既有 guard 块照旧
                    替换 answer——语义与现状一致，本层只负责"不再浪费 LLM 审核"）
  L2 review_llm     LLM 审核：query + 回答 + 检索依据 → 结构化 verdict（pass/reject）+
                    issues + feedback；**解析失败 fail-open 放行**（兼容旧 "PASS/理由"
                    文本格式——LLM 不按 JSON 输出也不至于卡死主链路）；
                    reject 且重试预算未尽 → validation_passed=False 走既有 generate 兜底
  L3 review_verify  定向回源核验：仅当 LLM 放行但答案存在**未回源引用**（引用的
                    《法名》+第X条 无法被 retrieved_docs 支撑——潜在幻觉信号）时，
                    经 ToolRegistry 调 pkulaw_verify(provision) 与权威原文对照；
                    确认引用错误 → 转 reject（走重试）；法宝不可用/预算耗尽/返回
                    形态不明 → **fail-open 放行**（核验故障绝不阻断主链路，D-M3-8 同款）

预算特性（区别于"每次回答都核验"的朴素做法）：正常回答的引用都有内部检索支撑，
pkulaw 调用次数为 0；只有疑似幻觉引用才消耗法宝积分（上限 REVIEW_VERIFY_MAX_CITATIONS
条/次）。LLM 层替换旧 validator 的一次调用，LLM 总量不变。

通信契约（M4 路线图 §3：子图产物写约定字段，不发消息）：
  state.review = {
      "verdict": "pass" | "reject" | "blocked",
      "layer": "rule" | "llm" | "verify",   # 产出最终 verdict 的层
      "issues": [...],                       # 问题清单（拒审理由/拦截原因/核验不一致项）
      "feedback": str,                       # 重试时喂给 generate 的修改意见
      "unbacked_citations": [...],           # 未被检索结果支撑的引用（verify 层触发依据）
      "verify_note": str,                    # 核验跳过/失败原因（fail-open 时留痕）
  }
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from langgraph.graph import StateGraph, END

from src.agents.state import AgentState
from src.agents.tools.registry import ToolRegistry
from src.memory.hallucination_guard import HallucinationGuard

logger = logging.getLogger(__name__)

# 单条引用的回灌内容窗口（送法宝核验时从答案中截取引用附近的原文）
_CITATION_WINDOW = 160
# 送审 LLM 的检索依据条数与单条摘要长度（与旧 VALIDATOR_PROMPT 同量级，控上下文）
_LLM_CTX_DOCS = 5
_LLM_CTX_SNIPPET = 160


REVIEW_SYSTEM = "你是一个法律回答审核员。只输出 JSON，不要输出任何其他内容。"

REVIEW_PROMPT = """审核以下法律回答是否合格。

## 用户问题
{query}

## 回答（被审对象）
{answer}

## 检索依据（系统检索到的条文摘要）
{context}

审核要点：
1. 回答是否依据检索到的条文作答，有无编造《法名》或条款号；
2. 核心结论有无明显法律错误或误导；
3. 是否遗漏了依据条文中的关键信息。

只输出如下 JSON（verdict 只能是 "pass" 或 "reject"）：
{{"verdict": "pass", "issues": [], "feedback": "一句话评语"}}"""


# ---------------------------------------------------------------------------
# 引用抽取与回源判定（纯函数，单测覆盖）
# ---------------------------------------------------------------------------

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

_CITATION_RE = re.compile(r"《([^《》]+)》[^《》]{0,40}?第([一二三四五六七八九十百千零\d]+)条")


def cn_to_int(numeral: str) -> int | None:
    """中文数字 → int（支持 一~万 与阿拉伯数字混排；无法解析返回 None）。"""
    s = (numeral or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, current = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total = (total + current) * 10000
                current = 0
            else:
                total += (current or 1) * unit
                current = 0
        else:
            return None
    return total + current


def article_key(article: str) -> str:
    """条号归一化：第251条 / 二百五十一 → 统一可比形态（能转数字转数字）。"""
    raw = (article or "").strip().removeprefix("第").removesuffix("条").strip()
    n = cn_to_int(raw)
    return str(n) if n is not None else raw


def law_key(law: str) -> str:
    """法名归一化：去书名号/空白/国号前缀/修订括注，如《中华人民共和国监察法(2024修正)》→ 监察法。"""
    s = (law or "").strip().strip("《》")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"\s+", "", s)
    if s.startswith("中华人民共和国"):
        s = s[len("中华人民共和国") :]
    return s


def extract_citations(answer: str) -> list[dict]:
    """抽取答案中「法名 + 条号」成对引用：《…》…第X条（裸条号噪声大，v1 不收）。"""
    out, seen = [], set()
    for m in _CITATION_RE.finditer(answer or ""):
        law, art = m.group(1).strip(), m.group(2)
        key = (law_key(law), article_key(art))
        if key in seen or not key[0] or not key[1]:
            continue
        seen.add(key)
        start = max(0, m.start() - _CITATION_WINDOW // 2)
        out.append(
            {
                "law": law,
                "article": f"第{art}条",
                "law_k": key[0],
                "article_k": key[1],
                "context": (answer or "")[start : m.end() + _CITATION_WINDOW // 2].strip(),
            }
        )
    return out


def _doc_law_keys(doc: dict) -> set[str]:
    """检索文档的法名候选键（全名/短名各收一个方向）。"""
    keys = set()
    for field in ("law_name", "title"):
        v = law_key(doc.get(field, "") or "")
        if v:
            keys.add(v)
            if v.startswith("中华人民共和国") is False:
                keys.add("中华人民共和国" + v)
    return keys


def citation_backed(cit: dict, docs: list[dict]) -> bool:
    """引用是否被检索结果支撑：法名能对上且条号出现在 article_range 或正文。"""
    for d in docs or []:
        if cit["law_k"] not in _doc_law_keys(d):
            continue
        art_text = f"{d.get('article_range', '') or ''}{d.get('content', '') or ''}"
        for m in re.finditer(r"第([一二三四五六七八九十百千零\d]+)条", art_text):
            if article_key(m.group(1)) == cit["article_k"]:
                return True
    return False


def find_unbacked_citations(answer: str, docs: list[dict]) -> list[dict]:
    """答案中被检索结果支撑不住的引用清单（潜在幻觉信号，L3 触发依据）。"""
    return [c for c in extract_citations(answer) if not citation_backed(c, docs or [])]


def parse_review_verdict(text: str) -> dict:
    """解析 LLM 审核输出。

    三级容错：① JSON（首选）；② 旧 validate 的 "PASS/理由" 文本格式（LLM 不守
    JSON 格式时的兜底，FakeToolLLM 等旧桩也依赖此路径）；③ 全部失败 → fail-open
    放行并留痕（审核故障不阻断主链路）。
    """
    text = (text or "").strip()
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict = str(obj.get("verdict", "")).lower()
            if verdict in ("pass", "reject"):
                issues = obj.get("issues") if isinstance(obj.get("issues"), list) else []
                return {
                    "verdict": verdict,
                    "issues": [str(i) for i in (issues or [])][:5],
                    "feedback": str(obj.get("feedback", ""))[:300],
                }
        except json.JSONDecodeError:
            pass
    upper = text.upper()
    if "PASS" in upper:
        return {"verdict": "pass", "issues": [], "feedback": ""}
    if "REJECT" in upper or "FAIL" in upper or "未通过" in text:
        reason = ""
        if "理由" in text:
            reason = text.split("理由", 1)[1].strip().lstrip("：:").strip()
        elif "\n" in text:
            reason = text.split("\n", 1)[1].strip()
        return {"verdict": "reject", "issues": [], "feedback": reason[:300]}
    logger.warning(f"审核输出无法解析，fail-open 放行: {text[:80]!r}")
    return {"verdict": "pass", "issues": [], "feedback": "", "unparsed": True}


def _verify_result_mismatch(data: dict) -> bool | None:
    """解读 pkulaw_verify(provision) 返回体：不一致 True / 明确一致 False / 形态不明 None。

    返回体形态不统一（SKILL 3.2），按字段语义取**明确的布尔标志**（match/consistent/
    all_match，顶层或逐条列表内）：任一 False → 不一致；全部 True → 一致；
    一个明确标志都没有 → None → 调用方 fail-open，绝不因形态不认识而误拒。
    """
    raw = (data or {}).get("result", data)
    if not isinstance(raw, dict):
        return None
    _FLAGS = ("match", "consistent", "all_match")
    explicit = [raw[k] for k in _FLAGS if isinstance(raw.get(k), bool)]
    if explicit:
        return False if any(explicit) else True  # 任一 False=不一致；全 True=一致
    items = [i for v in raw.values() if isinstance(v, list) for i in v if isinstance(i, dict)]
    flags = [i[k] for i in items for k in _FLAGS if isinstance(i.get(k), bool)]
    if flags:
        return False if any(flags) else True
    return None


# ---------------------------------------------------------------------------
# 三层节点与编译子图
# ---------------------------------------------------------------------------


def make_review_nodes(
    llm,
    registry: ToolRegistry,
    max_retries: int = 1,
    verify_max_citations: int = 3,
) -> dict[str, Callable]:
    """创建审核子图节点（闭包注入依赖，节点无状态）。

    与主 Agent 复用同一 llm（chat_with_tools 的重试/降级/预算语义在 chat 链路同样生效）；
    回源核验经 ToolRegistry.execute 走 pkulaw_verify 工具（预算埋点/降级语义零重复实现）。
    """

    def review_rule(state: AgentState) -> dict:
        """L1 规则守卫：拦截即短路（validation_passed=True 放行，外层 guard 替换 answer）。"""
        docs = state.get("retrieved_docs", []) or []
        answer = state.get("answer", "") or ""
        if not docs:
            # 与现状一致：无检索结果不触发 Layer1（ReAct 未检索属正常决策）
            return {"review": {"verdict": "pass", "layer": "rule", "issues": [], "feedback": ""}}
        guard = HallucinationGuard.guard(docs, answer)
        if guard["blocked"]:
            logger.info(f"审核 L1 规则守卫拦截: {guard['reason']}")
            return {
                "validation_passed": True,  # 语义与现状一致：拦截不重试，外层替换 answer
                "review": {
                    "verdict": "blocked",
                    "layer": "rule",
                    "issues": [guard["reason"]],
                    "feedback": guard["reason"],
                },
            }
        return {"review": {"verdict": "pass", "layer": "rule", "issues": [], "feedback": ""}}

    def review_llm(state: AgentState) -> dict:
        """L2 LLM 审核：结构化 verdict；reject 且预算未尽 → 旧重试语义。"""
        answer = state.get("answer", "") or ""
        query = state.get("query", "") or ""
        docs = state.get("retrieved_docs", []) or []
        ctx = "\n".join(
            f"- {d.get('citation', '')}: {(d.get('content', '') or '')[:_LLM_CTX_SNIPPET]}"
            for d in docs[:_LLM_CTX_DOCS]
        )
        parsed: dict[str, Any]
        try:
            resp = llm.chat(
                REVIEW_PROMPT.format(query=query[:500], answer=answer[:1200], context=ctx or "（无检索结果）"),
                system_prompt=REVIEW_SYSTEM,
            )
            parsed = parse_review_verdict(resp)
        except Exception as e:
            logger.warning(f"审核 LLM 调用失败，fail-open 放行: {type(e).__name__}: {e}")
            parsed = {"verdict": "pass", "issues": [], "feedback": "", "unparsed": True}

        review = {
            "layer": "llm",
            "issues": parsed.get("issues", []),
            "feedback": parsed.get("feedback", ""),
            "verdict": parsed["verdict"],
        }
        if parsed["verdict"] == "reject":
            retry = state.get("retry_count", 0) or 0
            if retry < max_retries:
                logger.info(f"审核 L2 拒绝，重试 {retry + 1}/{max_retries}: {parsed.get('feedback', '')[:80]}")
                review["verdict"] = "reject"
                return {
                    "validation_passed": False,
                    "retry_count": retry + 1,
                    "validation_feedback": parsed.get("feedback", "") or "回答未通过审核",
                    "review": review,
                }
            # 重试预算耗尽 → 强制放行（与旧 validate 语义一致），留痕
            review["verdict"] = "reject"
            review["forced"] = True

        # 放行：顺带算未回源引用（L3 触发依据，纯本地计算零成本）
        unbacked = find_unbacked_citations(answer, docs)
        review["unbacked_citations"] = [{"law": c["law"], "article": c["article"]} for c in unbacked]
        return {"validation_passed": True, "review": review}

    def review_verify(state: AgentState) -> dict:
        """L3 定向回源核验：仅审未回源引用；法宝故障 fail-open（绝不阻断）。"""
        review = dict(state.get("review") or {})
        unbacked = review.get("unbacked_citations") or []
        if not unbacked:
            return {"review": review}
        if not registry.has("pkulaw_verify"):
            review["verify_note"] = "pkulaw_verify 未注册（未启用/未配置），跳过回源核验"
            return {"review": review}

        answer = state.get("answer", "") or ""
        answerlaw = [
            {"title": c["law"], "article_number": c["article"], "text": c.get("context", "")}
            for c in extract_citations(answer)[:verify_max_citations]
            if {"law": c["law"], "article": c["article"]} in unbacked
        ][:verify_max_citations]
        if not answerlaw:
            return {"review": review}

        result = registry.execute(
            "pkulaw_verify",
            {"mode": "provision", "answerlaw": answerlaw, "userlaw": [], "prompt": "审核层引用核验"},
        )
        if not result.ok:
            # 额度耗尽/未配置/网络失败 → 核验不可用，放行留痕（不阻断主链路）
            review["verify_note"] = f"核验不可用: {result.summary}"
            logger.warning(f"审核 L3 回源核验不可用，fail-open 放行: {result.summary}")
            return {"review": review}

        mismatch = _verify_result_mismatch(result.data)
        if mismatch is True:
            retry = state.get("retry_count", 0) or 0
            note = "法宝核验发现引用与权威原文不一致"
            logger.warning(f"审核 L3 核验不一致: {note}")
            if retry < max_retries:
                return {
                    "validation_passed": False,
                    "retry_count": retry + 1,
                    "validation_feedback": note,
                    "review": {**review, "verdict": "reject", "layer": "verify", "issues": [note]},
                }
            review.update({"verdict": "reject", "layer": "verify", "issues": [note], "forced": True})
            return {"review": review}
        if mismatch is None:
            review["verify_note"] = "核验返回形态不明，按通过处理"
        else:
            review["verify_note"] = "已回源核验通过"
        return {"review": review}

    return {"review_rule": review_rule, "review_llm": review_llm, "review_verify": review_verify}


def route_after_rule(state: AgentState) -> str:
    """L1 → L2（未拦截）| END（拦截短路）。"""
    if (state.get("review") or {}).get("verdict") == "blocked":
        return END
    return "llm"


def route_after_llm(state: AgentState) -> str:
    """L2 → L3（放行但有未回源引用）| END（拒审待重试 / 无引用核验必要）。"""
    if not state.get("validation_passed", True):
        return END
    if (state.get("review") or {}).get("unbacked_citations"):
        return "verify"
    return END


def make_review_subgraph(
    llm,
    registry: ToolRegistry,
    max_retries: int = 1,
    verify_max_citations: int = 3,
):
    """编译审核子图：rule → llm → (verify) → END。

    用法（graph.py）：作为「validate」槽位的节点包装调用——
    `self._review_graph.invoke(state)` 后取 validation_passed/validation_feedback/
    retry_count/review 键合入 state。
    """
    nodes = make_review_nodes(llm, registry, max_retries, verify_max_citations)
    builder = StateGraph(AgentState)
    builder.add_node("rule", nodes["review_rule"])
    builder.add_node("llm", nodes["review_llm"])
    builder.add_node("verify", nodes["review_verify"])
    builder.set_entry_point("rule")
    builder.add_conditional_edges("rule", route_after_rule, {"llm": "llm", END: END})
    builder.add_conditional_edges("llm", route_after_llm, {"verify": "verify", END: END})
    builder.add_edge("verify", END)
    return builder.compile()
