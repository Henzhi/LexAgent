"""
Embedding 后端工厂函数。

根据环境变量配置自动选择并创建 Embedding 后端实例。
支持 Ollama 和 OpenAI 兼容 API 两种后端。
"""
from __future__ import annotations

import logging
import os

from src.embedding.base import EmbeddingBackend
from src.embedding.ollama_embedder import OllamaEmbedder
from src.embedding.openai_embedder import OpenAIEmbedder

logger = logging.getLogger(__name__)


def create_embedding_backend(
    backend_type: str | None = None,
    **kwargs,
) -> EmbeddingBackend:
    """创建 Embedding 后端实例

    根据 backend_type 或环境变量 EMBED_BACKEND 自动选择:
      - "ollama": OllamaEmbedder（本地部署）
      - "openai": OpenAIEmbedder（API 调用）

    若 EMBED_BACKEND 未设置，回退到 LLM_BACKEND 的值，
    再回退到 "ollama"。

    Args:
        backend_type: 后端类型
        **kwargs: 传给具体后端的参数

    Returns:
        EmbeddingBackend 实例
    """
    if backend_type is None:
        backend_type = os.getenv("EMBED_BACKEND") or "ollama"

    backend_type = backend_type.lower()

    if backend_type == "ollama":
        return _create_ollama(**kwargs)
    elif backend_type in ("openai", "openai_compatible"):
        return _create_openai(**kwargs)
    else:
        raise ValueError(
            f"不支持的 Embedding 后端类型: '{backend_type}'。"
            f"支持的类型: ollama, openai"
        )


def _create_ollama(**kwargs) -> OllamaEmbedder:
    model = kwargs.get("model") or os.getenv("EMBED_MODEL", "bge-m3")
    base_url = kwargs.get("base_url") or os.getenv("EMBED_BASE_URL", "http://localhost:11434")
    batch_size = kwargs.get("batch_size", int(os.getenv("EMBED_BATCH_SIZE", "32")))
    max_retries = kwargs.get("max_retries", int(os.getenv("EMBED_MAX_RETRIES", "3")))

    logger.info(f"创建 Ollama Embedding 后端: model={model}, base_url={base_url}")
    return OllamaEmbedder(
        model=model,
        base_url=base_url,
        batch_size=batch_size,
        max_retries=max_retries,
    )


def _create_openai(**kwargs) -> OpenAIEmbedder:
    model = kwargs.get("model") or os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    api_key = kwargs.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    base_url = kwargs.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    batch_size = kwargs.get("batch_size", int(os.getenv("EMBED_BATCH_SIZE", "32")))
    max_retries = kwargs.get("max_retries", int(os.getenv("EMBED_MAX_RETRIES", "3")))

    if not api_key:
        raise ValueError(
            "使用 OpenAI 兼容 Embedding 后端必须设置 OPENAI_API_KEY 环境变量"
        )

    safe_key = api_key[:8] + "..." if len(api_key) > 8 else "***"
    logger.info(f"创建 OpenAI Embedding 后端: model={model}, base_url={base_url}, api_key={safe_key}")
    return OpenAIEmbedder(
        model=model,
        api_key=api_key,
        base_url=base_url,
        batch_size=batch_size,
        max_retries=max_retries,
    )
