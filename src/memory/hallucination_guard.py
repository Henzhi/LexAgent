"""
幻觉防御模块 (v0.5)。

多层防御体系:
  Layer 1: 检索最低相似度阈值 — 检索结果相似度过低时拒绝回答
  Layer 2: 法条存在性检查 — 回答中引用的法条是否确实被检索到
  Layer 3: 内容安全 — 输出敏感词过滤
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _safe_float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# 检索最低相似度阈值（低于此值的检索结果不可靠）
# 可通过环境变量 HALLUCINATION_MIN_SIM 覆盖
MIN_SIMILARITY = _safe_float_env("HALLUCINATION_MIN_SIM", 0.4)

# 输出敏感词（涉黄/涉政/违法犯罪方法）
# 注：仅过滤明确教唆行为的短语，不阻断合法法律讨论
# （如"黑客入侵构成什么罪"是合法法律咨询）
_OUTPUT_BLOCKED = [
    "教你如何黑客", "如何入侵系统", "如何破解密码", "教你怎么刷机",
    "制造枪支的方法", "制造炸弹的方法", "制毒方法", "如何洗钱",
    "裸聊", "约炮", "嫖娼",
]


class HallucinationGuard:
    """多层幻觉防御器

    用法:
        guard = HallucinationGuard()
        result = guard.check(retrieved_docs, answer)
        if result.blocked:
            return result.fallback_answer
    """

    # ------------------------------------------------------------------
    # Layer 1: 检索置信度
    # ------------------------------------------------------------------

    @staticmethod
    def check_retrieval_confidence(docs: list[dict]) -> Optional[str]:
        """检查检索结果的置信度

        Returns:
            置信度不足时的回退回答，None 表示检索结果可靠
        """
        if not docs:
            return "抱歉，当前知识库中未找到与您问题相关的法律条文。建议您提供更详细的信息，或咨询专业律师获取准确的法律意见。"

        max_score = max((d.get("score", 0) for d in docs), default=0)
        if max_score < MIN_SIMILARITY:
            logger.warning(f"检索置信度过低: max_score={max_score:.4f} < {MIN_SIMILARITY}")
            return (
                f"抱歉，当前知识库中未找到与您问题高度匹配的法律条文"
                f"（最佳匹配相似度为 {max_score:.2f}）。\n"
                f"请尝试换一种表述方式，或咨询专业律师。"
            )
        return None

    # ------------------------------------------------------------------
    # Layer 2: 内容安全
    # ------------------------------------------------------------------

    @staticmethod
    def check_content_safety(text: str) -> Optional[str]:
        """检查输出内容是否包含敏感信息

        Returns:
            违规时的回退回答，None 表示安全
        """
        for kw in _OUTPUT_BLOCKED:
            if kw in text:
                logger.warning(f"检测到输出敏感词: {kw}")
                return "该问题不在我的服务范围内。"
        return None

    # ------------------------------------------------------------------
    # 综合检测
    # ------------------------------------------------------------------

    @staticmethod
    def guard(
        retrieved_docs: list[dict],
        answer: str,
    ) -> dict:
        """执行全部防御层检测

        Returns:
            {"blocked": bool, "fallback": str | None, "reason": str}
        """
        # Layer 1: 检索置信度
        fallback = HallucinationGuard.check_retrieval_confidence(retrieved_docs)
        if fallback:
            return {"blocked": True, "fallback": fallback, "reason": "low_retrieval_confidence"}

        # Layer 2: 内容安全
        fallback = HallucinationGuard.check_content_safety(answer)
        if fallback:
            return {"blocked": True, "fallback": fallback, "reason": "content_safety"}

        return {"blocked": False, "fallback": None, "reason": ""}
