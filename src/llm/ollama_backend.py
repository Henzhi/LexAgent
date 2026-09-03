"""
Ollama LLM 后端实现。

通过 ollama Python SDK 调用本地 Ollama 服务，支持同步和流式调用。

重试策略（src.llm.retry）:
  - 仅重试可重试异常（429 / 5xx / 网络 / 超时），4xx 业务错误直接抛出
  - 指数退避 + 全抖动 + 尊重 Retry-After 头
  - 流式请求已产出内容后失败不再重试（避免重复 token），
    并在 finally 中尽力关闭底层流以尽快释放连接
"""

from __future__ import annotations

import logging
from typing import Iterator

import ollama
from langchain_ollama import ChatOllama

from src.llm.base import (
    LLMBackend,
    ToolCallResponse,
    to_langchain_messages,
    tool_calls_from_langchain,
)
from src.llm.budget_callback import budget_callbacks
from src.llm.usage_callback import usage_callbacks
from src.llm.retry import is_retryable, wait_and_log

logger = logging.getLogger(__name__)

# 上下文窗口映射: 模型名 → token数
# 说明: 窗口应取模型真实上下文能力且略保守(留 KV Cache 余量)，
#       与 Ollama 服务端 num_ctx 保持一致；可用 OLLAMA_NUM_CTX 显式覆盖。
_OLLAMA_CONTEXT_WINDOWS = {
    "qwen2.5:3b": 32000,  # 真实 32768，取保守值
    "qwen2.5:7b": 28000,
    "qwen2.5:14b": 60000,
    "qwen2.5:32b": 80000,
    "qwen2.5:72b": 80000,
    "qwen3:8b": 32000,
    "llama3.1:8b": 32000,
    "deepseek-r1:7b": 32000,
    "deepseek-r1:14b": 64000,
}


class OllamaBackend(LLMBackend):
    """Ollama LLM 后端

    用法:
        backend = OllamaBackend(model="qwen2.5:7b", base_url="http://localhost:11434")
        reply = backend.chat("请解释一下什么是不正当竞争")
        for token in backend.chat_stream("请解释..."):
            print(token, end="")
    """

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        top_p: float = 0.9,
        max_tokens: int = 2048,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        repeat_penalty: float = 1.05,
        seed: int = 42,
        num_ctx: int = 0,
    ):
        super().__init__(
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        self.base_url = base_url
        self.repeat_penalty = repeat_penalty
        self.seed = seed
        # 请求上下文窗口：0 = 自动使用模型声明窗口（get_context_window()）。
        # Ollama 服务端 num_ctx 默认仅 2048，不显式下发会静默截断输入。
        self.num_ctx = num_ctx or self.get_context_window()
        self._client = self._init_client()
        # D-M3-13：LangChain 标准 ChatModel，供 bind_tools / invoke / stream 使用
        self._model = self._init_model()

    def _init_client(self) -> ollama.Client:
        host = self.base_url.replace("http://", "").replace("https://", "")
        return ollama.Client(host=host, timeout=300.0)

    def _init_model(self) -> ChatOllama:
        """LangChain 标准 ChatModel（D-M3-13）。

        `num_ctx` 必须显式下发：Ollama 服务端默认仅 2048，不指定会静默截断输入
        （这行注释来自迁移前的实现，是个真实的坑，别丢）。
        """
        return ChatOllama(
            model=self.model,
            base_url=self.base_url,
            temperature=self.temperature,
            top_p=self.top_p,
            num_predict=self.max_tokens,
            num_ctx=self.num_ctx,
            repeat_penalty=self.repeat_penalty,
            seed=self.seed,
            callbacks=[*budget_callbacks(), *usage_callbacks(backend="ollama", model=self.model)],
        )

    def get_context_window(self) -> int:
        return _OLLAMA_CONTEXT_WINDOWS.get(self.model, 28000)

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
                    logger.warning(f"Ollama 调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self.max_retries, logger_name=__name__)

        raise RuntimeError(f"Ollama 调用失败，已重试 {self.max_retries} 次: {last_error}")

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
                # 调用方中断（客户端断开 / 桥接取消）：立即停止，不重试
                raise
            except Exception as e:
                last_error = e
                if yielded_any:
                    logger.warning(f"Ollama 流式中途失败（已输出内容，不重试）: {e}")
                    raise
                if not is_retryable(e):
                    logger.warning(f"Ollama 流式调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self.max_retries, logger_name=__name__)

        raise RuntimeError(f"Ollama 流式调用失败，已重试 {self.max_retries} 次: {last_error}")

    # ------------------------------------------------------------------
    # 工具调用实现（M1 / F2）
    # ------------------------------------------------------------------

    def _chat_with_tools_impl(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        """Ollama 工具调用（非流式）：LangChain `bind_tools()` + `invoke()`。

        - 模型不支持工具 / 未返回 tool_calls → 返回空列表 + content，上层据此直接生成。
        - D-M1-4：小模型 tool calling 不可靠，上层在 Ollama 降级时走固定管线，
          本方法只在显式调用时才走到。
        - LangChain 的 tool_calls 参数是已解析的 dict，无需 JSON 容错。

        注：Ollama 0.4.x 的 SDK 不支持 tool_choice，LangChain ChatOllama 同样
        忽略该参数，因此这里不传（保持与迁移前一致的行为）。
        """
        lc_messages = to_langchain_messages(messages)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                model = self._model.bind_tools(tools) if tools else self._model
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
                    logger.warning(f"Ollama 工具调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self.max_retries, logger_name=__name__)

        raise RuntimeError(f"Ollama 工具调用失败，已重试 {self.max_retries} 次: {last_error}")
