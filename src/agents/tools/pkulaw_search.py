"""
pkulaw_search / pkulaw_verify 内置工具（M3+ / F9 扩展，决策 D-PKULAW）。

将北大法宝 MCP 暴露为 Agent 可自主调用的工具：
- pkulaw_search：检索类——法条语义检索 / 精确取条 / 类案语义检索 / 法规列表；
- pkulaw_verify：核验与呈现类——法条识别 / 案号识别 / 法条正文对照 / 批量加超链
  （反幻觉闭环，对应 pkulaw SKILL 第七节）。

与 legal_source_search（后端自动融合）互补：本工具让 Agent 在需要**精确取条、
类案原文、引用核验、可点击链接**时显式调用，能力更细。

失败处理（与现有工具一致，不中断 ReAct 循环）：
- 客户端不可用 / 调用异常 → ToolResult(ok=False)，summary 首词"法宝检索失败"；
- 预算额度用尽 → ToolResult(ok=False)，summary 首词"法宝额度已用尽"（内部库与
  网络仍可用，回答照常生成，符合 REQ-UW1 语义）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from src.agents.tools.base import (
    CATEGORY_LEGAL,
    SOURCE_LEGAL,
    ToolResult,
    ToolSpec,
    tool,
    truncate_summary,
)
from src.observability.cost_budget import BudgetExceededError
from src.search.pkulaw_mcp import PkulawMCPClient

logger = logging.getLogger(__name__)

_SUMMARY_MAX_ITEMS = 3


def _build_search_summary(items: list[dict]) -> str:
    """检索结果摘要：命中数 + 前几条（title — 状态）。"""
    if not items:
        return "北大法宝未检索到相关结果"
    parts = [f"检索到 {len(items)} 条北大法宝结果（官方源，已验证）："]
    for i, r in enumerate(items[:_SUMMARY_MAX_ITEMS], start=1):
        title = (r.get("title") or "").strip() or "无标题"
        status = (r.get("law_status") or "").strip()
        extra = f"（{status}）" if status else ""
        parts.append(f"{i}) {title}{extra}")
    return truncate_summary("；".join(parts))


def build_pkulaw_search_spec(client: PkulawMCPClient | None = None) -> ToolSpec:
    """构造 pkulaw_search 工具（检索类）。依赖经闭包注入。"""

    pk = client or PkulawMCPClient()

    @tool(name="pkulaw_search", category=CATEGORY_LEGAL)
    def pkulaw_search(
        mode: Annotated[
            Literal["article_search", "article_exact", "case_search", "law_list"],
            "检索模式：article_search=法条语义检索；article_exact=精确取条"
            "（须给 title+number）；case_search=类案语义检索；law_list=法规列表",
        ],
        query: Annotated[str, "检索关键词（article_search/case_search/law_list 用）"] = "",
        title: Annotated[str, "法规名（article_exact/law_list 用），如 '民法典'"] = "",
        number: Annotated[str, "条号（article_exact 用），中文或数字均可，如 '第一千零七十七条'"] = "",
        lib: Annotated[str, "法条检索辖区，默认 '中央'（避免地方文件）"] = "中央",
        effectiveness: Annotated[
            list[str] | None, "法规列表效力位阶筛选（law_list 用），如 ['法律','行政法规']"
        ] = None,
        top_k: Annotated[int, "返回条数，默认 5"] = 5,
    ) -> ToolResult:
        """检索北大法宝官方法律权威源（法条原文 / 类案全文 / 法规目录）。

        用于：确认法条是否现行有效与原文、查找类案裁判规则、列合规清单。
        结果为官方源（verified_official），可信度高于网络搜索；
        网络线索需以本结果二次验证。法条语义检索建议带 lib='中央'。
        """
        # top_k 是 LLM 生成的参数，等同不可信输入：钳制到 [1, 50]，防止
        # LLM 传 100000 之类把法宝端点/响应体打爆（2026-09-01 审查整改）
        try:
            top_k = max(1, min(int(top_k), 50))
        except (TypeError, ValueError):
            top_k = 5
        if not pk.is_available():
            return ToolResult(
                tool="pkulaw_search",
                call_id="",
                ok=False,
                summary="法宝检索失败：北大法宝未配置（URL/Token 缺失或 SDK 未安装）",
                data={},
                source=SOURCE_LEGAL,
            )
        try:
            if mode == "article_search":
                items = pk.search_article(query, lib=lib, max_results=top_k)
            elif mode == "article_exact":
                items = pk.get_article(title, number)
            elif mode == "case_search":
                items = pk.search_case(query, max_results=top_k)
            elif mode == "law_list":
                items = pk.get_law_list(title, effectiveness=effectiveness or None, max_results=top_k)
            else:
                return ToolResult(
                    tool="pkulaw_search",
                    call_id="",
                    ok=False,
                    summary=f"参数校验失败: 未知 mode={mode}",
                    data={},
                    source=SOURCE_LEGAL,
                )
            return ToolResult(
                tool="pkulaw_search",
                call_id="",
                ok=True,
                summary=_build_search_summary(items),
                data={"results": items, "count": len(items), "mode": mode},
                source=SOURCE_LEGAL,
            )
        except BudgetExceededError as e:
            logger.warning(f"pkulaw_search 当日预算已用尽: {e}")
            return ToolResult(
                tool="pkulaw_search",
                call_id="",
                ok=False,
                summary=f"法宝额度已用尽: {e}",
                data={},
                source=SOURCE_LEGAL,
            )
        except Exception as e:
            logger.warning(f"pkulaw_search 检索失败（法宝检索失败）: {e}")
            return ToolResult(
                tool="pkulaw_search",
                call_id="",
                ok=False,
                summary=f"法宝检索失败: {e}",
                data={},
                source=SOURCE_LEGAL,
            )

    return pkulaw_search


def build_pkulaw_verify_spec(client: PkulawMCPClient | None = None) -> ToolSpec:
    """构造 pkulaw_verify 工具（核验与呈现类）。依赖经闭包注入。"""

    pk = client or PkulawMCPClient()

    @tool(name="pkulaw_verify", category=CATEGORY_LEGAL)
    def pkulaw_verify(
        mode: Annotated[
            Literal["law_name", "case_number", "provision", "add_links"],
            "核验模式：law_name=法条识别（抽法规名并标准化）；case_number=案号识别"
            "（验真伪）；provision=法条正文对照（AI 自写 vs 权威原文）；add_links=批量加超链",
        ],
        text: Annotated[str, "待核验/待处理的整段文本（law_name/case_number/add_links 用）"] = "",
        userlaw: Annotated[
            list[dict] | None, "法条核验-待核对条文，每项 {title, article_number, text}（provision 用）"
        ] = None,
        answerlaw: Annotated[
            list[dict] | None, "法条核验-AI 自写条文，每项 {title, article_number, text}（provision 用）"
        ] = None,
        prompt: Annotated[str, "法条核验补充说明（可选）"] = "",
    ) -> ToolResult:
        """核验法律引用真伪与加可点击链接（反幻觉闭环）。

        高风险输出（意见书/合同/对外文书）或用户要求核验时调用：
        - law_name：抽取一段文本引用的法规名并标准化（不返回时效）；
        - case_number：验证文本中案号能否在库查到（字段为空=未验证到，非编造）；
        - provision：将 AI 自写条文与北大法宝权威原文逐条对照；
        - add_links：给整段文本批量加 pkulaw 原文链接（最后一步，仅 Markdown 环境）。
        """
        if not pk.is_available():
            return ToolResult(
                tool="pkulaw_verify",
                call_id="",
                ok=False,
                summary="法宝核验失败：北大法宝未配置（URL/Token 缺失或 SDK 未安装）",
                data={},
                source=SOURCE_LEGAL,
            )
        try:
            if mode == "law_name":
                raw = pk.verify_law(text)
            elif mode == "case_number":
                raw = pk.verify_case(text)
            elif mode == "provision":
                # None 兜底：默认值改为 None（可变默认参数 [] 跨请求共享，审查整改）
                raw = pk.verify_provision(userlaw=userlaw or [], answerlaw=answerlaw or [], prompt=prompt)
            elif mode == "add_links":
                linked = pk.add_links(text)
                return ToolResult(
                    tool="pkulaw_verify",
                    call_id="",
                    ok=True,
                    summary=truncate_summary(f"已加链（{len(linked)} 字）"),
                    data={"linked": linked, "mode": mode},
                    source=SOURCE_LEGAL,
                )
            else:
                return ToolResult(
                    tool="pkulaw_verify",
                    call_id="",
                    ok=False,
                    summary=f"参数校验失败: 未知 mode={mode}",
                    data={},
                    source=SOURCE_LEGAL,
                )
            return ToolResult(
                tool="pkulaw_verify",
                call_id="",
                ok=True,
                summary=truncate_summary(f"北大法宝核验完成（{mode}）"),
                data={"result": raw, "mode": mode},
                source=SOURCE_LEGAL,
            )
        except BudgetExceededError as e:
            logger.warning(f"pkulaw_verify 当日预算已用尽: {e}")
            return ToolResult(
                tool="pkulaw_verify",
                call_id="",
                ok=False,
                summary=f"法宝额度已用尽: {e}",
                data={},
                source=SOURCE_LEGAL,
            )
        except Exception as e:
            logger.warning(f"pkulaw_verify 核验失败（法宝核验失败）: {e}")
            return ToolResult(
                tool="pkulaw_verify",
                call_id="",
                ok=False,
                summary=f"法宝核验失败: {e}",
                data={},
                source=SOURCE_LEGAL,
            )

    return pkulaw_verify
