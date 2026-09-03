"""
OpenAI 兼容 API LLM 后端实现。

支持所有兼容 OpenAI Chat Completions API 的服务:
  - OpenAI (gpt-4o, gpt-4o-mini)
  - DeepSeek (deepseek-chat, deepseek-reasoner)
  - 通义千问 (qwen-turbo, qwen-plus, qwen-max)
  - 本地 vLLM / Ollama OpenAI 兼容端点
  - 其他兼容服务

通过 openai Python SDK 调用，支持同步和流式。

重试策略（src.llm.retry）:
  - 仅重试可重试异常（429 / 5xx / 网络 / 超时），4xx 业务错误直接抛出
  - 指数退避 + 全抖动 + 尊重 Retry-After 头，避免 429 惊群放大限流
  - 流式请求已产出内容后失败不再重试（避免重复 token / 重复计费），
    并在 finally 中关闭底层流以尽快释放连接
  - 重试耗尽抛 `LLMRetryExhaustedError`（携带最后一次状态码），供 Failover
    判定降级（2026-09-01 审查整改 B1：此前抛裸 RuntimeError 导致不降级）
"""

from __future__ import annotations

import logging
from typing import Iterator

from langchain_openai import ChatOpenAI
from openai import OpenAI

from src.llm.base import (
    LLMBackend,
    ToolCallResponse,
    to_langchain_messages,
    tool_calls_from_langchain,
)
from src.llm.budget_callback import budget_callbacks
from src.llm.usage_callback import usage_callbacks
from src.llm.retry import (
    LLMRetryExhaustedError,
    is_retryable,
    wait_and_log,
)

logger = logging.getLogger(__name__)

# 上下文窗口映射
_OPENAI_CONTEXT_WINDOWS = {
    "gpt-4o": 120000,
    "gpt-4o-mini": 120000,
    "gpt-4-turbo": 120000,
    "gpt-3.5-turbo": 16000,
    "deepseek-chat": 60000,
    "deepseek-v4-flash": 32000,
    "deepseek-reasoner": 60000,
    "qwen-turbo": 32000,
    "qwen-plus": 32000,
    "qwen-max": 32000,
    "qwen2.5:7b": 28000,
    "qwen2.5:14b": 60000,
}


class OpenAICompatibleBackend(LLMBackend):
    """OpenAI 兼容 API LLM 后端

    用法:
        backend = OpenAICompatibleBackend(
            model="deepseek-chat",
            api_key="sk-xxx",
            base_url="https://api.deepseek.com/v1",
        )
        reply = backend.chat("请解释一下什么是不正当竞争")
        for token in backend.chat_stream("请解释..."):
            print(token, end="")
    """

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str = "",
        base_url: str = "https://api.deepseek.com/v1",
        temperature: float = 0.1,
        top_p: float = 0.9,
        max_tokens: int = 2048,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        super().__init__(
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        self.api_key = api_key
        self.base_url = base_url
        self._client = self._init_client()
        # D-M3-13：LangChain 标准 ChatModel，供 bind_tools / invoke / stream 使用
        self._model = self._init_model()

    def _init_client(self) -> OpenAI:
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=0,  # 我们自己管理重试
            timeout=120.0,
        )

    def _init_model(self) -> ChatOpenAI:
        """LangChain 标准 ChatModel（D-M3-13）。

        `max_retries=0`：重试仍由本模块的 `is_retryable` + `wait_and_log` 统一控制，
        保持 D-M1-3 的策略——429/5xx/网络 可重试，4xx 业务错误直接抛出交由
        Failover 降级（4xx 重试必然失败，浪费请求）。
        """
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key or "sk-placeholder",
            base_url=self.base_url,
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            max_retries=0,  # 重试由我们自己管理
            timeout=120.0,
            stream_usage=True,  # F15：流式也要 usage_metadata（DeepSeek 兼容 include_usage）
            callbacks=[*budget_callbacks(), *usage_callbacks(backend="deepseek", model=self.model)],
        )

    def get_context_window(self) -> int:
        return _OPENAI_CONTEXT_WINDOWS.get(self.model, 32000)

    # ------------------------------------------------------------------
    # LLMBackend 抽象方法实现
    # ------------------------------------------------------------------

    def _generate_impl(self, messages: list[dict[str, str]]) -> str:
        lc_messages = to_langchain_messages(messages)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._model.invoke(lc_messages)
                return resp.content or ""
            except Exception as e:
                last_error = e
                if not is_retryable(e):
                    logger.warning(f"OpenAI API 调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self.max_retries, logger_name=__name__)

        raise LLMRetryExhaustedError(f"OpenAI API 调用失败，已重试 {self.max_retries} 次: {last_error}", last_error)

    def _stream_impl(self, messages: list[dict[str, str]]) -> Iterator[str]:
        lc_messages = to_langchain_messages(messages)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            yielded_any = False
            try:
                for chunk in self._model.stream(lc_messages):
                    text = getattr(chunk, "content", "") or ""
                    if text:
                        yielded_any = True
                        yield text
                return
            except GeneratorExit:
                # 调用方中断（客户端断开 / 桥接取消）：立即停止，不重试。
                # LangChain 的 stream 是生成器，GeneratorExit 会沿调用链关闭底层连接。
                raise
            except Exception as e:
                last_error = e
                if yielded_any:
                    # 已向用户输出过内容，不能从头重试（会重复 / 重复计费），
                    # 直接抛给上层处理。
                    logger.warning(f"OpenAI API 流式中途失败（已输出内容，不重试）: {e}")
                    raise
                if not is_retryable(e):
                    logger.warning(f"OpenAI API 流式调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self.max_retries, logger_name=__name__)

        raise LLMRetryExhaustedError(f"OpenAI API 流式调用失败，已重试 {self.max_retries} 次: {last_error}", last_error)

    # ------------------------------------------------------------------
    # 工具调用实现（M1 / F2，D-M3-13 改为 LangChain bind_tools）
    # ------------------------------------------------------------------

    def _chat_with_tools_impl(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        """非流式工具调用：LangChain `bind_tools()` + `invoke()`。

        - DeepSeek V4 parallel_tool_calls 恒启用：返回的多个 tool_calls 全部解析。
        - 仅在有工具时 bind_tools（空数组可能被部分供应商拒绝）。
        - 空 name 的占位 tool_call 由 `tool_calls_from_langchain` 跳过（D-M1-6）。
        - 重试策略与普通调用一致：仅可重试异常（429/5xx/网络/超时）重试。

        与迁移前的行为差异：LangChain 的 tool_calls 参数是**已解析的 dict**，
        不像 OpenAI 原始响应那样是 JSON 字符串，无需参数 JSON 容错层。
        """
        lc_messages = to_langchain_messages(messages)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                model = self._model.bind_tools(tools, tool_choice=tool_choice) if tools else self._model
                resp = model.invoke(lc_messages)
                raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
                return ToolCallResponse(
                    content=resp.content or "",
                    tool_calls=tool_calls_from_langchain(resp),
                    raw=raw,
                )
            except Exception as e:
                last_error = e
                if not is_retryable(e):
                    logger.warning(f"OpenAI API 工具调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self.max_retries, logger_name=__name__)

        raise LLMRetryExhaustedError(f"OpenAI API 工具调用失败，已重试 {self.max_retries} 次: {last_error}", last_error)
