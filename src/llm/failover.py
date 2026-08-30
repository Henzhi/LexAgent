"""
主备双后端降级组合（M1 / F5 / REQ-U1 / REQ-UW2）。

设计（架构师 D5）：
- 组合主后端（OpenAI 兼容 / DeepSeek）与备用后端（Ollama），对外保持 LLMBackend 接口不变。
- 创建期：主后端初始化失败（缺 Key / 网络不可达）→ 工厂直接构造已降级实例（degraded=True）。
- 运行期：主后端调用抛出**不可重试**异常（4xx / 认证失败）→ 切换备用后端并记录降级标记；
  可重试异常（429 / 5xx / 网络 / 超时）仍由 src.llm.retry 在底层后端内部处理，不触发降级。
- 降级标记通过 `degraded` / `active_backend` 属性透出，供图/SSE 层展示"当前使用降级模型"。

线程安全：降级切换用锁保护，多线程并发首败时只降级一次。
"""

from __future__ import annotations

import logging
import threading
from typing import Iterator

from src.llm.base import LLMBackend, ToolCallResponse

logger = logging.getLogger(__name__)


def _backend_label(backend: LLMBackend) -> str:
    """从后端类型名推导标签（openai / ollama / 其他）。"""
    name = type(backend).__name__.lower()
    if "ollama" in name:
        return "ollama"
    if "openai" in name:
        return "openai"
    return name.replace("backend", "").strip("_") or "unknown"


def _is_non_retryable_api_error(exc: BaseException) -> bool:
    """判断是否为主后端不可恢复的 API 错误（4xx 业务/鉴权错误）。

    规则：
    - 429 / 5xx 可重试（由底层后端 retry.py 处理），不触发降级；
    - 4xx（400/401/403/404/422 等，408 除外）属业务/鉴权错误 → 触发降级；
    - 无 HTTP 状态码的异常（如重试耗尽后的 RuntimeError、编程错误）不触发降级，
      避免把可恢复或内部错误误判为"后端不可用"。
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if isinstance(status, int):
        return 400 <= status < 500 and status not in (408, 429)
    return False


class FailoverLLMBackend(LLMBackend):
    """主备双后端降级组合。

    用法:
        backend = FailoverLLMBackend(primary=openai_backend, fallback=ollama_backend)
        reply = backend.chat("请解释一下什么是不正当竞争")
        if backend.degraded:
            print("当前使用降级模型")
    """

    def __init__(self, primary: LLMBackend | None, fallback: LLMBackend):
        """初始化主备后端。

        Args:
            primary: 主后端（OpenAI 兼容）。为 None 表示创建期已降级，直接走备用。
            fallback: 备用后端（Ollama），必选。
        """
        if fallback is None:
            raise ValueError("FailoverLLMBackend 必须提供备用后端 fallback")
        self.primary = primary
        self.fallback = fallback
        self._degraded = primary is None
        self._lock = threading.Lock()
        active = primary or fallback
        super().__init__(
            model=active.model,
            temperature=active.temperature,
            top_p=active.top_p,
            max_tokens=active.max_tokens,
            max_retries=active.max_retries,
            retry_delay=getattr(active, "retry_delay", 2.0),
        )
        if self._degraded:
            logger.warning("[failover] 主后端缺失，初始即使用备用后端")

    # ------------------------------------------------------------------
    # 降级状态
    # ------------------------------------------------------------------

    @property
    def active_backend(self) -> str:
        """当前生效的后端标签：openai | ollama。"""
        active = self.fallback if self._degraded else (self.primary or self.fallback)
        return _backend_label(active)

    @property
    def degraded(self) -> bool:
        """是否已降级到备用后端。"""
        return self._degraded

    @property
    def chat_model(self):
        """当前生效后端的 LangChain ChatModel（D-M3-13）。

        降级后要指向备用后端的 model，因此每次访问动态解析，不在构造期固定。
        """
        active = self.fallback if self._degraded else (self.primary or self.fallback)
        return active.chat_model

    def mark_degraded(self, reason: str) -> None:
        """创建期降级标记（由工厂在构造失败时调用）。"""
        with self._lock:
            if not self._degraded:
                self._degraded = True
                logger.warning(f"[failover] 降级为备用后端: {reason}")

    def _switch_to_fallback(self, exc: BaseException) -> None:
        """运行期切换备用后端（幂等，仅首次生效）。"""
        with self._lock:
            if self._degraded:
                return
            self._degraded = True
            logger.warning(f"[failover] 主后端调用失败（不可重试），切换到备用后端 {self.active_backend}: {exc}")

    # ------------------------------------------------------------------
    # LLMBackend 接口实现
    # ------------------------------------------------------------------

    def get_context_window(self) -> int:
        active = self.fallback if self._degraded else (self.primary or self.fallback)
        return active.get_context_window()

    def _active(self) -> LLMBackend:
        """当前生效的后端。"""
        return self.fallback if self._degraded else (self.primary or self.fallback)

    def _generate_impl(self, messages: list[dict[str, str]]) -> str:
        """兜底实现（满足 ABC）：委托给当前生效后端。

        正常路径经 chat()/chat_with_tools() 已按降级语义分发，此处仅作安全兜底。
        """
        return self._active()._generate_impl(messages)

    def _stream_impl(self, messages: list[dict[str, str]]) -> Iterator[str]:
        """兜底实现（满足 ABC）：委托给当前生效后端。"""
        yield from self._active()._stream_impl(messages)

    def chat(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        if self._degraded or self.primary is None:
            return self.fallback.chat(user_message, history=history, system_prompt=system_prompt)
        try:
            return self.primary.chat(user_message, history=history, system_prompt=system_prompt)
        except Exception as e:
            if _is_non_retryable_api_error(e):
                self._switch_to_fallback(e)
                return self.fallback.chat(user_message, history=history, system_prompt=system_prompt)
            raise

    def chat_stream(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> Iterator[str]:
        if self._degraded or self.primary is None:
            yield from self.fallback.chat_stream(user_message, history=history, system_prompt=system_prompt)
            return
        yielded_any = False
        try:
            for token in self.primary.chat_stream(user_message, history=history, system_prompt=system_prompt):
                yielded_any = True
                yield token
        except Exception as e:
            # 已产出内容则不降级重放（会重复 token / 重复计费），直接抛出
            if not yielded_any and _is_non_retryable_api_error(e):
                self._switch_to_fallback(e)
                yield from self.fallback.chat_stream(user_message, history=history, system_prompt=system_prompt)
                return
            raise

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        if self._degraded or self.primary is None:
            return self.fallback.chat_with_tools(messages, tools, tool_choice)
        try:
            return self.primary.chat_with_tools(messages, tools, tool_choice)
        except Exception as e:
            if _is_non_retryable_api_error(e):
                self._switch_to_fallback(e)
                return self.fallback.chat_with_tools(messages, tools, tool_choice)
            raise
