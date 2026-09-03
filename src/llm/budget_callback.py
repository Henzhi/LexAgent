"""
F14 预算熔断的 LangChain callback 埋点（D-M3-13）。

**为什么需要它**：LLM 层迁移到 LangChain 后，调用入口不再只有 `LLMBackend` 的
三个公开方法——上层可以直接 `llm.chat_model.bind_tools(...).invoke(...)`，
原来的显式埋点会被绕过。改为 `BaseCallbackHandler` 后，所有经 LangChain 的调用
都会被计数，无论走哪个入口。

**与 D-M3-6 的口径保持一致**：
- 预占（熔断）在调用发起前：`on_llm_start` —— 原子预占，超限即中断
- 调用成功：`on_llm_end` 不再计数（配额已在发起时预占，重复计数会把限额腰斩）
- 调用失败：`on_llm_error` 归还预占（请求未真正完成不占配额）
- 统计组件故障一律告警放行，不拖垮主链路（D-M3-8）

**为什么不用 LangChain 的 on_llm_new_token 计数**：流式一次调用会产生成百上千个
token 回调，而预算口径是「逻辑调用次数」，必须每次调用只计一次。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from src.observability.cost_budget import (
    KIND_LLM,
    BudgetExceededError,
    get_budget,
)

logger = logging.getLogger(__name__)


class LLMBudgetCallbackHandler(BaseCallbackHandler):
    """LLM 调用的预算检查与用量计数（F14）。

    用法（两个后端构造 ChatModel 时挂载）：

        ChatOpenAI(..., callbacks=[LLMBudgetCallbackHandler()])
    """

    # ⚠️ 关键（2026-09-01 代码审查修复）：BaseCallbackHandler 默认 raise_error=False，
    # LangChain 的 CallbackManager 会 try/except 捕获 handler 异常后仅 logger.warning
    # 后继续（langchain_core/callbacks/manager.py）。不开这一行，`on_llm_start` 抛出的
    # BudgetExceededError 会被静默吞掉——单次请求内 18~20 次 LLM 调用全部放行，
    # F14 只剩请求入口的 `_budget_block_message()` 前置检查兜底。
    raise_error = True

    @property
    def name(self) -> str:
        return "llm_budget_callback"

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """调用发起前**原子预占**配额：超限 → 抛 BudgetExceededError 熔断。

        注意：这里**不能**吞掉 BudgetExceededError——它是熔断信号，
        必须让 LangChain 中断调用链，由 API 层转成友好提示。

        为什么是预占而不是事后计数（2026-09-03 审查整改）：一次请求内有
        18~20 次 LLM 调用，并发流的 check 与 record 之间存在 TOCTOU 窗口，
        日限额会被放大到 limit + (并发数-1)。预占把拦截点移到花钱之前——
        被限流的请求拿到的是友好提示，而不是答案生成到一半被截断。
        """
        try:
            get_budget().check_and_reserve(KIND_LLM)
        except BudgetExceededError:
            raise
        except Exception as e:
            # 统计组件自身故障（Redis 抖动等）不应影响主链路
            logger.warning(f"预算检查失败（放行）: {e}")

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """调用成功 → 配额已在 on_llm_start 预占，此处无需再计数。

        ⚠️ 不要在这里再调 `record()`：会与预占重复计数，日限额被腰斩。
        """
        return None

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """调用失败 → 归还预占的配额（与 D-M3-6「请求未真正完成不占配额」一致）。"""
        try:
            get_budget().release(KIND_LLM)
        except Exception as e:
            logger.warning(f"预算配额归还失败（忽略）: {e}")
        return None


def budget_callbacks() -> list[BaseCallbackHandler]:
    """构造 ChatModel 时挂载的预算 callback 列表。

    单独抽出是为了让豁免场景（如预算自检、管理接口）可以选择不挂载。
    """
    return [LLMBudgetCallbackHandler()]
