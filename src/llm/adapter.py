"""
LLM / Embedding 后端适配器。

将新的 LLMBackend / EmbeddingBackend 适配为旧 API，使现有代码
（RAGEngine、LawAgentGraph、PgvectorStoreRetriever 等）无需改动即可使用新后端。

过渡方案：Phase 1 阶段使用，后续逐步替换为直接使用新 API。
"""
from __future__ import annotations

from typing import Any, Iterator, List

from src.llm.base import LLMBackend
from src.embedding.base import EmbeddingBackend


def _normalize_history(history: list[Any] | None) -> list[dict[str, str]] | None:
    """将旧 LLMMessage 对象列表转换为 dict 列表，兼容新后端。

    旧的 LawLLM 和 graph.py 使用 LLMMessage(role, content) 对象，
    新的 LLMBackend._build_messages() 期望 {"role": ..., "content": ...} dict。
    """
    if history is None:
        return None
    result = []
    for msg in history:
        if hasattr(msg, "role") and hasattr(msg, "content"):
            result.append({"role": str(msg.role), "content": str(msg.content)})
        elif isinstance(msg, dict):
            result.append(msg)
        else:
            result.append({"role": "user", "content": str(msg)})
    return result


# ---------------------------------------------------------------------------
# LLM 适配器 — 兼容旧 LawLLM 接口
# ---------------------------------------------------------------------------

class LLMAdapter:
    """将 LLMBackend 适配为旧 LawLLM 兼容接口

    提供 chat_with_context / chat_stream_with_context 等旧 API，
    内部委托给新后端。
    """

    def __init__(self, backend: LLMBackend):
        self._backend = backend
        self.model_name = backend.model
        self.temperature = backend.temperature

    @property
    def chat_model(self):
        """底层 LangChain ChatModel（D-M3-13）——委托给被包装的后端。

        供标准生态互操作（bind_tools / invoke / stream）直接使用；但 ReAct 循环的
        决策调用必须走 `chat_with_tools()` 公开入口（重试与 Failover 降级语义在
        该入口链路上，绕过会丢失，见 react_nodes.agent_node）。
        """
        return self._backend.chat_model

    # ----- 基础 API -----

    def chat(
        self,
        user_message: str,
        history: list | None = None,
        system_prompt: str | None = None,
    ) -> str:
        return self._backend.chat(
            user_message,
            history=_normalize_history(history),
            system_prompt=system_prompt,
        )

    def chat_stream(
        self,
        user_message: str,
        history: list | None = None,
        system_prompt: str | None = None,
    ) -> Iterator[str]:
        yield from self._backend.chat_stream(
            user_message,
            history=_normalize_history(history),
            system_prompt=system_prompt,
        )

    # ----- 工具调用 API（M1 / F2）-----

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ):
        """带工具调用能力的对话（委托给后端，支持 FailoverLLMBackend 降级）。"""
        return self._backend.chat_with_tools(messages, tools, tool_choice)

    @property
    def degraded(self) -> bool:
        """后端是否已降级（FailoverLLMBackend 专用；普通后端恒为 False）。"""
        return bool(getattr(self._backend, "degraded", False))

    # ----- RAG 上下文 API（兼容旧 LawLLM）-----

    def chat_with_context(
        self,
        user_message: str,
        context_docs: str,
        history: list | None = None,
    ) -> str:
        prompt = self._backend._build_rag_prompt(user_message, context_docs)
        return self.chat(prompt, history)

    def chat_stream_with_context(
        self,
        user_message: str,
        context_docs: str,
        history: list | None = None,
    ) -> Iterator[str]:
        prompt = self._backend._build_rag_prompt(user_message, context_docs)
        yield from self.chat_stream(prompt, history)

    # ----- 上下文窗口 -----

    def get_context_window(self) -> int:
        return self._backend.get_context_window()


# ---------------------------------------------------------------------------
# Embedding 适配器 — 兼容旧 LawEmbedder 接口
# ---------------------------------------------------------------------------

class EmbeddingAdapter:
    """将 EmbeddingBackend 适配为旧 LawEmbedder 兼容接口

    实现 LangChain Embeddings 接口，可直接用于 pgvector 向量库。
    """

    def __init__(self, backend: EmbeddingBackend):
        self._backend = backend
        self.model = backend.model
        self.batch_size = backend.batch_size

    # ----- 旧 API -----

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._backend.embed(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._backend.embed_query(text)

    def embed_documents_with_progress(
        self, texts: List[str], show_progress: bool = True
    ) -> List[List[float]]:
        return self._backend.embed_with_progress(texts, show_progress)

    def get_embedding_dim(self) -> int:
        return self._backend.get_dimension()
