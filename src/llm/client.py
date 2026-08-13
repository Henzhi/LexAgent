"""
Ollama LLM 客户端。

基于 ollama Python SDK，实现 LangChain BaseChatModel 接口，
支持同步/流式调用、自动重试、历史上下文管理。

用法示例:
    client = LawLLM()
    reply = client.chat("请解释一下什么是不正当竞争")
    for chunk in client.chat_stream("请解释..."):
        print(chunk, end="")
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

import ollama
from langchain_core.callbacks import CallbackManagerForLLMRun

from src.llm.retry import is_retryable, wait_and_log
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 消息模型
# ---------------------------------------------------------------------------

class Message:
    """轻量消息封装（非 LangChain 用户直接使用）"""

    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    @staticmethod
    def system(content: str) -> Message:
        return Message("system", content)

    @staticmethod
    def user(content: str) -> Message:
        return Message("user", content)

    @staticmethod
    def assistant(content: str) -> Message:
        return Message("assistant", content)


# ---------------------------------------------------------------------------
# LLM 配置
# ---------------------------------------------------------------------------

class LLMConfig:
    """LLM 调用参数"""

    def __init__(
        self,
        temperature: float = 0.1,
        top_p: float = 0.9,
        num_predict: int = 2048,
        repeat_penalty: float = 1.05,
        seed: int = 42,
    ):
        self.temperature = temperature
        self.top_p = top_p
        self.num_predict = num_predict
        self.repeat_penalty = repeat_penalty
        self.seed = seed

    def to_options(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "num_predict": self.num_predict,
            "repeat_penalty": self.repeat_penalty,
            "seed": self.seed,
        }


# ---------------------------------------------------------------------------
# 系统提示词模板
# ---------------------------------------------------------------------------

LAW_SYSTEM_PROMPT = """你是一位专业的中国法律助手，具备以下能力：

1. 引用具体的法律条文回答用户问题
2. 根据用户提供的历史消息上下文中的法律条文进行推理
3. 回答简洁准确，优先使用法律原文
4. 如果被问及法律条文中没有涉及的内容，明确指出缺乏依据
5. 用中文回答，条理清晰"""


# ---------------------------------------------------------------------------
# 核心客户端
# ---------------------------------------------------------------------------

class LawLLM(BaseChatModel):
    """Ollama 法律 LLM 客户端

    同时满足：
    - 直接使用（简洁 API）
    - LangChain Agent 集成（实现 BaseChatModel）
    """

    # ----- LangChain 元数据 -----
    model_name: str = "qwen2.5:7b"
    temperature: float = 0.1

    # ----- 内部配置 -----
    _client: ollama.Client | None = None
    _base_url: str = "http://localhost:11434"
    _config: LLMConfig | None = None
    _system_prompt: str = ""
    _max_retries: int = 3
    _retry_delay: float = 2.0

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        system_prompt: str = LAW_SYSTEM_PROMPT,
        config: LLMConfig | None = None,
        max_retries: int = 3,
    ):
        super().__init__(model_name=model, temperature=config.temperature if config else 0.1)
        self._base_url = base_url
        self._config = config or LLMConfig()
        self._system_prompt = system_prompt
        self._max_retries = max_retries
        self._init_client()

    def _init_client(self) -> None:
        host = self._base_url.replace("http://", "").replace("https://", "")
        self._client = ollama.Client(host=host, timeout=300.0)  # 本地 LLM 冷启动较慢，给 5 分钟

    # ------------------------------------------------------------------
    # 简洁 API（直接使用，不依赖 LangChain）
    # ------------------------------------------------------------------

    def chat(
        self,
        user_message: str,
        history: list[Message] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """单轮对话

        Args:
            user_message: 用户消息
            history: 历史对话消息列表
            system_prompt: 系统提示词（默认使用 LAW_SYSTEM_PROMPT）

        Returns:
            LLM 响应文本
        """
        messages = self._build_messages(user_message, history, system_prompt)
        return self._call_ollama(messages)

    def chat_stream(
        self,
        user_message: str,
        history: list[Message] | None = None,
        system_prompt: str | None = None,
    ) -> Iterator[str]:
        """流式对话

        Yields:
            逐个 token 的输出文本
        """
        messages = self._build_messages(user_message, history, system_prompt)
        yield from self._call_ollama_stream(messages)

    def chat_with_context(
        self,
        user_message: str,
        context_docs: str,
        history: list[Message] | None = None,
    ) -> str:
        """带法律条文上下文的问答（RAG 用）"""
        prompt = self._build_rag_prompt(user_message, context_docs)
        return self.chat(prompt, history)

    def chat_stream_with_context(
        self,
        user_message: str,
        context_docs: str,
        history: list[Message] | None = None,
    ) -> Iterator[str]:
        """带法律条文上下文的流式问答（RAG 用）"""
        prompt = self._build_rag_prompt(user_message, context_docs)
        yield from self.chat_stream(prompt, history)

    # ------------------------------------------------------------------
    # LangChain BaseChatModel 接口（Agent / LangGraph 集成）
    # ------------------------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """LangChain 同步调用入口"""
        ollama_msgs = self._langchain_to_ollama(messages)
        response = self._call_ollama(ollama_msgs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=response))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """LangChain 流式调用入口"""
        ollama_msgs = self._langchain_to_ollama(messages)
        for token_text in self._call_ollama_stream(ollama_msgs):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=token_text))
            if run_manager:
                run_manager.on_llm_new_token(token_text, chunk=chunk)
            yield chunk

    @property
    def _llm_type(self) -> str:
        return "ollama-law-llm"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "temperature": self.temperature,
            "base_url": self._base_url,
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_message: str,
        history: list[Message] | None = None,
        system_prompt: str | None = None,
    ) -> list[dict[str, str]]:
        """构建 Ollama API 消息列表"""
        sp = system_prompt or self._system_prompt
        messages: list[dict[str, str]] = []

        if sp:
            messages.append({"role": "system", "content": sp})

        if history:
            for msg in history:
                messages.append(msg.to_dict())

        messages.append({"role": "user", "content": user_message})
        return messages

    def _build_rag_prompt(self, query: str, context: str) -> str:
        """构建 RAG 问答 prompt"""
        return f"""请根据以下法律条文回答用户的问题。

## 相关法律条文
{context}

## 用户问题
{query}

## 要求
1. 回答中必须引用具体的法律条文（注明法律名称和条款号）
2. 如果条文中没有直接答案，指出现有条文的规定和相关联的情况
3. 保持回答简洁，不要凭空编造"""

    def _call_ollama(self, messages: list[dict[str, str]]) -> str:
        """调用 Ollama chat API（带重试）

        重试策略见 src.llm.retry：仅重试 429/5xx/网络/超时，指数退避+抖动。
        """
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.chat(
                    model=self.model_name,
                    messages=messages,
                    options=self._config.to_options(),
                )
                return response["message"]["content"]
            except Exception as e:
                last_error = e
                if not is_retryable(e):
                    logger.warning(f"Ollama 调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self._max_retries, logger_name=__name__)

        raise RuntimeError(
            f"Ollama 调用失败，已重试 {self._max_retries} 次: {last_error}"
        )

    def _call_ollama_stream(
        self, messages: list[dict[str, str]]
    ) -> Iterator[str]:
        """流式调用 Ollama chat API（带重试）

        已产出内容后失败不重试（避免重复 token），并关闭底层流尽快释放连接。
        """
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            stream = None
            yielded_any = False
            try:
                stream = self._client.chat(
                    model=self.model_name,
                    messages=messages,
                    options=self._config.to_options(),
                    stream=True,
                )
                for chunk in stream:
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yielded_any = True
                        yield content
                return
            except GeneratorExit:
                raise
            except Exception as e:
                last_error = e
                if yielded_any:
                    logger.warning(
                        f"Ollama 流式中途失败（已输出内容，不重试）: {e}"
                    )
                    raise
                if not is_retryable(e):
                    logger.warning(f"Ollama 流式调用失败（不可重试）: {e}")
                    raise
                wait_and_log(e, attempt, self._max_retries, logger_name=__name__)
            finally:
                if stream is not None:
                    try:
                        close = getattr(stream, "close", None)
                        if callable(close):
                            close()
                    except Exception:
                        pass

        raise RuntimeError(
            f"Ollama 流式调用失败，已重试 {self._max_retries} 次: {last_error}"
        )

    @staticmethod
    def _langchain_to_ollama(
        messages: list[BaseMessage],
    ) -> list[dict[str, str]]:
        """LangChain 消息 → Ollama 消息格式"""
        result = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                result.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                result.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                result.append({"role": "assistant", "content": msg.content})
            else:
                result.append({"role": "user", "content": str(msg.content)})
        return result


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------

def create_llm(
    model: str = "qwen2.5:7b",
    base_url: str = "http://localhost:11434",
    temperature: float = 0.1,
) -> LawLLM:
    """快速创建 LLM 客户端"""
    return LawLLM(
        model=model,
        base_url=base_url,
        config=LLMConfig(temperature=temperature),
    )
