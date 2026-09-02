"""
主备双后端降级组合（M1 / F5 / REQ-U1 / REQ-UW2）。

设计（架构师 D5）：
- 组合主后端（OpenAI 兼容 / DeepSeek）与备用后端（Ollama），对外保持 LLMBackend 接口不变。
- 创建期：主后端初始化失败（缺 Key / 网络不可达）→ 工厂直接构造已降级实例（degraded=True）。
- 运行期：主后端调用抛出**不可重试**异常（4xx / 认证失败）→ 切换备用后端并记录降级标记；
  可重试异常（429 / 5xx / 网络 / 超时）仍由 src.llm.retry 在底层后端内部处理，不触发降级；
  重试**耗尽**后抛 `LLMRetryExhaustedError` → 触发降级（B1）。
- 降级标记通过 `degraded` / `active_backend` 属性透出，供图/SSE 层展示"当前使用降级模型"。
- **自动回切**（2026-09-01 审查整改 B2）：降级后进入冷却窗口（默认
  `DEFAULT_RECOVERY_COOLDOWN_SECONDS` = 300s）；冷却结束后下一次**真实请求**
  即作为健康探测走主后端，成功则回切、失败则继续降级并刷新冷却窗口。
  - 为什么不为探测单独发一个 ping 请求：那要为"确认后端活着"付出真实 Token
    与一次 RTT；复用下一次真实请求零成本，且探测失败时请求仍由备用端应答，
    用户无感。

线程安全：降级切换用锁保护，多线程并发首败时只降级一次。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterator

from src.llm.base import LLMBackend, ToolCallResponse
from src.llm.retry import LLMRetryExhaustedError

logger = logging.getLogger(__name__)

# 降级后多久才允许下一次健康探测（秒）。设 0 表示禁用自动回切，
# 保持"降级后不再回头"的旧语义。
DEFAULT_RECOVERY_COOLDOWN_SECONDS = 300.0


def _backend_label(backend: LLMBackend) -> str:
    """从后端类型名推导标签（openai / ollama / 其他）。"""
    name = type(backend).__name__.lower()
    if "ollama" in name:
        return "ollama"
    if "openai" in name:
        return "openai"
    return name.replace("backend", "").strip("_") or "unknown"


def _is_non_retryable_api_error(exc: BaseException) -> bool:
    """判断是否为主后端不可恢复的错误（4xx 业务/鉴权错误 或 重试耗尽）。

    规则：
    - 429 / 5xx 可重试（由底层后端 retry.py 处理），不触发降级；
    - 4xx（400/401/403/404/422 等，408 除外）属业务/鉴权错误 → 触发降级；
    - `LLMRetryExhaustedError`（重试耗尽哨兵，B1）→ 触发降级：持续 429/5xx
      把重试次数用完，说明主后端在当前窗口内确实不可用，有备用就该用上；
    - 其他无 HTTP 状态码的异常（编程错误等）不触发降级，避免把内部 bug 误判
      成"后端不可用"而永久降级。
    """
    if isinstance(exc, LLMRetryExhaustedError):
        return True
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

    def __init__(
        self,
        primary: LLMBackend | None,
        fallback: LLMBackend,
        recovery_cooldown_seconds: float = DEFAULT_RECOVERY_COOLDOWN_SECONDS,
    ):
        """初始化主备后端。

        Args:
            primary: 主后端（OpenAI 兼容）。为 None 表示创建期已降级，直接走备用。
            fallback: 备用后端（Ollama），必选。
            recovery_cooldown_seconds: 降级后到下一次健康探测的冷却秒数（B2）。
                0 或负数表示禁用自动回切。
        """
        if fallback is None:
            raise ValueError("FailoverLLMBackend 必须提供备用后端 fallback")
        self.primary = primary
        self.fallback = fallback
        self._degraded = primary is None
        self._recovery_cooldown = float(recovery_cooldown_seconds or 0.0)
        # 最近一次进入降级态的时刻（monotonic）；仅 _degraded=True 时有意义
        self._degraded_at = time.monotonic() if self._degraded else 0.0
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

    @property
    def recovery_cooldown_seconds(self) -> float:
        """降级后到下一次健康探测的冷却秒数（0 = 禁用自动回切）。"""
        return self._recovery_cooldown

    def mark_degraded(self, reason: str) -> None:
        """创建期降级标记（由工厂在构造失败时调用）。"""
        with self._lock:
            if not self._degraded:
                self._degraded = True
                self._degraded_at = time.monotonic()
                logger.warning(f"[failover] 降级为备用后端: {reason}")

    def _switch_to_fallback(self, exc: BaseException) -> None:
        """运行期切换备用后端（幂等；已降级时刷新冷却窗口起点）。"""
        with self._lock:
            already = self._degraded
            self._degraded = True
            self._degraded_at = time.monotonic()
            if already:
                logger.warning(f"[failover] 主后端健康探测失败，继续降级并重置冷却窗口: {exc}")
            else:
                logger.warning(f"[failover] 主后端调用失败（不可重试），切换到备用后端 {self.active_backend}: {exc}")

    # ------------------------------------------------------------------
    # 健康探测与回切（B2）
    # ------------------------------------------------------------------

    def _cooldown_elapsed(self) -> bool:
        """降级后的冷却窗口是否已过（未降级 / 无主后端 / 禁用回切 → False）。"""
        if not self._degraded or self.primary is None:
            return False
        if self._recovery_cooldown <= 0:
            return False
        return (time.monotonic() - self._degraded_at) >= self._recovery_cooldown

    def _try_primary(self) -> bool:
        """本次调用是否应尝试主后端。

        - 未降级：正常走主后端；
        - 已降级且冷却已过：本次调用兼作健康探测（成功即回切）；
        - 已降级且冷却未过：直接走备用，不浪费一次对故障后端的调用。
        """
        if self.primary is None:
            return False
        return (not self._degraded) or self._cooldown_elapsed()

    def _recover(self) -> None:
        """主后端探测成功 → 回切（幂等）。"""
        with self._lock:
            if self._degraded:
                self._degraded = False
                self._degraded_at = 0.0
                logger.info("[failover] 主后端健康探测成功，已回切到主后端")

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
        if not self._try_primary():
            return self.fallback.chat(user_message, history=history, system_prompt=system_prompt)
        probing = self._degraded  # 降级态下的这次调用兼作健康探测（B2）
        try:
            result = self.primary.chat(  # type: ignore[union-attr]
                user_message, history=history, system_prompt=system_prompt
            )
        except Exception as e:
            # 探测失败（任何异常）→ 继续降级并由备用端应答：用户已在降级态，
            # 没有理由因为探测失败而中断这次请求。
            if probing or _is_non_retryable_api_error(e):
                self._switch_to_fallback(e)
                return self.fallback.chat(user_message, history=history, system_prompt=system_prompt)
            raise
        if probing:
            self._recover()
        return result

    def chat_stream(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> Iterator[str]:
        if not self._try_primary():
            yield from self.fallback.chat_stream(user_message, history=history, system_prompt=system_prompt)
            return
        probing = self._degraded
        yielded_any = False
        try:
            for token in self.primary.chat_stream(  # type: ignore[union-attr]
                user_message, history=history, system_prompt=system_prompt
            ):
                yielded_any = True
                yield token
        except GeneratorExit:
            # 调用方中断（客户端断开）：探测未得出结论，不改变降级状态
            raise
        except Exception as e:
            # 已产出内容则不降级重放（会重复 token / 重复计费），直接抛出
            if yielded_any:
                raise
            if probing or _is_non_retryable_api_error(e):
                self._switch_to_fallback(e)
                yield from self.fallback.chat_stream(user_message, history=history, system_prompt=system_prompt)
                return
            raise
        if probing:
            self._recover()

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        if not self._try_primary():
            return self.fallback.chat_with_tools(messages, tools, tool_choice)
        probing = self._degraded
        try:
            result = self.primary.chat_with_tools(messages, tools, tool_choice)  # type: ignore[union-attr]
        except Exception as e:
            if probing or _is_non_retryable_api_error(e):
                self._switch_to_fallback(e)
                return self.fallback.chat_with_tools(messages, tools, tool_choice)
            raise
        if probing:
            self._recover()
        return result
