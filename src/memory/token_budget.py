"""
Token 预算管理器 (v0.5)。

管理上下文窗口的 Token 分配，确保 Prompt 不超过模型限制。
支持动态分配（根据查询复杂度调整各段占比）和压缩策略。

用法:
    budget = TokenBudget(context_window=28000)
    budget.consume("system", system_prompt)
    budget.consume("retrieval", docs_text, max_tokens=8000)
    prompt = budget.build(history, memory_ctx, query)
"""
from __future__ import annotations

import logging

import tiktoken

logger = logging.getLogger(__name__)

# 编码器（qwen2.5 / gpt-4o 用 cl100k_base，DeepSeek 也用相同编码）
_ENCODING = tiktoken.get_encoding("cl100k_base")

# 默认分配（28K 窗口）
DEFAULT_ALLOCATION = {
    "system_prompt":  {"tokens": 800,   "priority": "required", "compressible": False},
    "memory_context": {"tokens": 1500,  "priority": "high",     "compressible": True},
    "retrieval_docs": {"tokens": 8000,  "priority": "highest",  "compressible": True},
    "chat_history":   {"tokens": 3000,  "priority": "medium",   "compressible": True},
    "output_reserve": {"tokens": 12000, "priority": "required", "compressible": False},
}


class TokenBudget:
    """Token 预算管理器

    根据上下文窗口大小和查询复杂度动态分配各段 Token。
    超限时按优先级从低到高压缩 / 截断。
    """

    def __init__(self, context_window: int = 28000):
        self.total = context_window - 2000  # 预留 2K 安全边界
        self._used: dict[str, int] = {}
        self._segments: dict[str, str] = {}
        self._allocation = dict(DEFAULT_ALLOCATION)

    # ------------------------------------------------------------------
    # Token 计数
    # ------------------------------------------------------------------

    @staticmethod
    def count(text: str) -> int:
        """计算文本 Token 数"""
        return len(_ENCODING.encode(text))

    @staticmethod
    def count_batch(texts: list[str]) -> int:
        """批量计算 Token 数"""
        total = 0
        for t in texts:
            total += len(_ENCODING.encode(t))
        return total

    # ------------------------------------------------------------------
    # 分配策略
    # ------------------------------------------------------------------

    def adjust_for_complexity(self, query: str):
        """根据查询复杂度动态调整分配比例

        - 对比分析（含"对比/区别/哪个"）：优先，放大检索预算
        - 简单查询（≤20字）：缩小检索预算，放大历史对话
        """
        q_len = len(query)

        # 1. 对比分析优先（短对比查询如"A和B有什么区别"）
        if any(kw in query for kw in ["对比", "区别", "哪个", "vs", "比较"]):
            self._allocation["retrieval_docs"]["tokens"] = 10000
            self._allocation["memory_context"]["tokens"] = 500
            self._allocation["chat_history"]["tokens"] = 500
            self._allocation["output_reserve"]["tokens"] = 13000
            return

        # 2. 简单查询
        if q_len <= 20:
            self._allocation["retrieval_docs"]["tokens"] = 3000
            self._allocation["chat_history"]["tokens"] = 4000
            return

    # ------------------------------------------------------------------
    # 消耗与构建
    # ------------------------------------------------------------------

    def consume(self, name: str, text: str, max_tokens: int | None = None) -> str:
        """向预算中消耗一段内容

        Args:
            name: 段名（对应 DEFAULT_ALLOCATION key）
            text: 文本内容
            max_tokens: 最大 Token 数（None=用默认分配）

        Returns:
            截断/压缩后的文本
        """
        alloc = self._allocation.get(name, {"tokens": 0, "compressible": True})
        limit = max_tokens or alloc["tokens"]

        tokens = self.count(text)
        if tokens <= limit:
            self._used[name] = tokens
            self._segments[name] = text
            return text

        # 超限 → 截断
        self._used[name] = limit
        truncated = self._truncate(text, limit)
        self._segments[name] = truncated
        logger.debug(f"TokenBudget: {name} {tokens}→{limit} 已截断")
        return truncated

    def build(
        self,
        system_prompt: str,
        retrieval_text: str,
        history_text: str,
        memory_text: str,
        query: str,
    ) -> str:
        """组装最终 Prompt，确保不超窗口

        Returns:
            完整的 prompt 文本
        """
        self.consume("system_prompt", system_prompt)
        self.consume("memory_context", memory_text)
        self.consume("retrieval_docs", retrieval_text)
        self.consume("chat_history", history_text)

        # 剩余空间给输出
        used = sum(self._used.values()) + self.count(query)
        reserve = self.total - used
        if reserve < 1000:
            logger.warning(f"TokenBudget: 仅剩 {reserve} tokens 给输出，可能过短")

        parts = [
            self._segments.get("system_prompt", ""),
            self._segments.get("memory_context", ""),
            self._segments.get("retrieval_docs", ""),
            self._segments.get("chat_history", ""),
            f"\n## 用户问题\n{query}",
        ]
        return "\n\n".join(p for p in parts if p)

    def get_limit(self, name: str) -> int:
        """获取某段的预算上限 (tokens)"""
        alloc = self._allocation.get(name)
        return alloc["tokens"] if alloc else 0

    def build_template(self, template: str, query: str) -> str:
        """用预算截断后的各段填充现有模板

        Args:
            template: 含 {context} / {query} 占位符的提示词模板
            query: 用户问题

        Returns:
            组装后的 prompt 文本
        """
        ctx = self._segments.get("retrieval_docs", "")
        return template.format(context=ctx, query=query)

    def used_ratio(self) -> float:
        total = sum(self._used.values())
        return total / self.total

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate(text: str, max_tokens: int) -> str:
        tokens = _ENCODING.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _ENCODING.decode(tokens[:max_tokens])
