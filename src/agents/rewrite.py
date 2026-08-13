"""查询改写：将用户口语化表述规范化为法律检索查询。

设计原则（法律系统）：
- 仅做术语规范化，不改变原意，不引入未提及的法律概念。
- 大众不会说法言法语，故改写对召回必要；但改写结果必须由用户确认，
  将"算法黑箱"转为"人机协作"，宁可牺牲部分召回也要保证精度。
- 本模块不写入 LangGraph（因确认需要人机往返），而是作为前端开关控制
  的前置步骤（/api/rewrite）独立调用。
"""
from __future__ import annotations

import logging
import re

from src.agents.prompts import REWRITE_PROMPT

logger = logging.getLogger(__name__)

# 明确法条/法律名引用（如 "第232条"、《民法典》）— 改写结果必须原样保留
_ARTICLE_REF = re.compile(r"第[0-9０-９零一二三四五六七八九十百千两]+条")
_LAW_REF = re.compile(r"《[^》]+》")


def rewrite_query(llm, query: str) -> str:
    """用 LLM 将 query 改写为规范法律检索语句。失败则回退原句。

    精度护栏：原句中的明确法条引用（第X条 / 《法律名》）若在改写结果中
    丢失，说明 LLM 改变了检索意图（如把"刑法第232条是什么"泛化成
    "故意杀人罪"），此时回退原句以保证法条精确查找。
    """
    prompt = REWRITE_PROMPT.format(query=query)
    try:
        out = llm.chat(prompt)
    except Exception as e:
        logger.warning("改写失败，回退原句: %s", e)
        return query
    out = (out or "").strip()
    # 去掉模型可能加的引号 / 书名号外壳
    if len(out) >= 2 and out[0] in "\"“'‘" and out[-1] in "\"”'’":
        out = out[1:-1].strip()
    # 去掉模型可能加的前缀（如 "改写："、"回答："）
    for prefix in ("改写：", "改写:", "改写", "回答：", "回答:", "结果：", "结果:"):
        if out.startswith(prefix):
            out = out[len(prefix):].strip()
            break
    # 去掉残留换行 / 多余空白
    out = " ".join(out.split())
    if not out:
        return query
    # 精度护栏：法条引用丢失 → 回退原句
    refs = _ARTICLE_REF.findall(query) + _LAW_REF.findall(query)
    if refs and any(ref not in out for ref in refs):
        logger.warning("改写丢失法条引用 %s（%r → %r），回退原句", refs, query, out)
        return query
    return out
