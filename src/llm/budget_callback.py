"""
F14 预算熔断的 LangChain callback 埋点（D-M3-13）。

**为什么需要它**：LLM 层迁移到 LangChain 后，调用入口不再只有 `LLMBackend` 的
三个公开方法——上层可以直接 `llm.chat_model.bind_tools(...).invoke(...)`，
原来的显式埋点会被绕过。改为 `BaseCallbackHandler` 后，所有经 LangChain 的调用
都会被计数，无论走哪个入口。

**与 D-M3-6 的口径保持一致**：
- 检查（熔断）在调用发起前：`on_llm_start`
- 计数在调用完成后：`on_llm_end` —— 失败不计数（请求未真正完成）
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

    @property
    def name(self) -> str:
        return "llm_budget_callback"

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """调用发起前检查预算：超限 → 抛 BudgetExceededError 熔断。

        注意：这里**不能**吞掉 BudgetExceededError——它是熔断信号，
        必须让 LangChain 中断调用链，由 API 层转成友好提示。
        """
        try:
            get_budget().check(KIND_LLM)
        except BudgetExceededError:
            raise
        except Exception as e:
            # 统计组件自身故障（Redis 抖动等）不应影响主链路
            logger.warning(f"预算检查失败（放行）: {e}")

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """调用完成 → 计数 1 次。"""
        try:
            get_budget().record(KIND_LLM)
        except Exception as e:
            logger.warning(f"预算计数失败（忽略）: {e}")

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """调用失败不计数（与 D-M3-6「请求未真正完成不占配额」一致）。"""
        return None


def budget_callbacks() -> list[BaseCallbackHandler]:
    """构造 ChatModel 时挂载的预算 callback 列表。

    单独抽出是为了让豁免场景（如预算自检、管理接口）可以选择不挂载。
    """
    return [LLMBudgetCallbackHandler()]
