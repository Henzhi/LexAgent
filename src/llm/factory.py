"""
LLM 后端工厂函数。

根据环境变量配置自动选择并创建 LLM 后端实例。
支持 Ollama 和 OpenAI 兼容 API 两种后端。

M1（F5）：`create_llm_backend(failover=True)` 构建 FailoverLLMBackend
（主 openai + 备 ollama），主后端创建失败或运行期不可重试异常时自动降级。
"""

from __future__ import annotations

import logging
import os

from src.llm.base import LLMBackend
from src.llm.failover import FailoverLLMBackend
from src.llm.ollama_backend import OllamaBackend
from src.llm.openai_backend import OpenAICompatibleBackend

logger = logging.getLogger(__name__)

# 默认系统提示词
LAW_SYSTEM_PROMPT = """你是一位专业的中国法律助手，具备以下能力：

1. 引用具体的法律条文回答用户问题
2. 根据用户提供的历史消息上下文中的法律条文进行推理
3. 回答简洁准确，优先使用法律原文
4. 如果被问及法律条文中没有涉及的内容，明确指出缺乏依据
5. 用中文回答，条理清晰"""


def create_llm_backend(
    backend_type: str | None = None,
    failover: bool = False,
    **kwargs,
) -> LLMBackend:
    """创建 LLM 后端实例

    根据 backend_type 或环境变量 LLM_BACKEND 自动选择后端:

    - "ollama": OllamaBackend（本地部署）
    - "openai": OpenAICompatibleBackend（API 调用）

    Args:
        backend_type: 后端类型，为 None 时从环境变量 LLM_BACKEND 读取
        failover: 是否构建主备降级组合（主 openai + 备 ollama）。
                  为 True 时忽略 backend_type，始终以 OpenAI 兼容为主、Ollama 为备。
        **kwargs: 传给具体后端的参数

    Returns:
        LLMBackend 实例（failover=True 时为 FailoverLLMBackend）
    """
    if backend_type is None:
        backend_type = os.getenv("LLM_BACKEND", "openai")

    backend_type = backend_type.lower()

    if failover:
        return _create_failover(**kwargs)

    if backend_type == "ollama":
        return _create_ollama(**kwargs)
    elif backend_type in ("openai", "openai_compatible"):
        return _create_openai(**kwargs)
    else:
        raise ValueError(f"不支持的 LLM 后端类型: '{backend_type}'。支持的类型: ollama, openai")


def _create_failover(**kwargs) -> FailoverLLMBackend:
    """创建主备降级组合：主 OpenAI（DeepSeek）+ 备 Ollama（默认）。

    - 主后端创建失败（缺 OPENAI_API_KEY / SDK 初始化异常）→ 降级为仅备用后端；
    - 备用后端创建失败且主后端可用 → 退回单一主后端；
    - 两者均失败 → 抛出 RuntimeError。
    - 备用后端类型由 LLM_FALLBACK_BACKEND 控制（默认 ollama）。
    """
    from src.config import (
        LLM_FALLBACK_BACKEND,
        LLM_FALLBACK_MODEL,
        LLM_FALLBACK_BASE_URL,
        LLM_MAX_TOKENS,
        LLM_MAX_RETRIES,
        LLM_TEMPERATURE,
        LLM_TOP_P,
    )

    primary: OpenAICompatibleBackend | None = None
    primary_error: Exception | None = None
    try:
        primary = _create_openai(**kwargs)
    except Exception as e:  # 缺 Key / SDK 初始化失败等
        primary_error = e
        logger.warning(f"[failover] 主后端（OpenAI/DeepSeek）创建失败，将回退 Ollama: {e}")

    fallback_model = kwargs.get("fallback_model") or LLM_FALLBACK_MODEL
    fallback_base_url = kwargs.get("fallback_base_url") or LLM_FALLBACK_BASE_URL
    fallback_backend = (kwargs.get("fallback_backend") or LLM_FALLBACK_BACKEND).lower()
    common = dict(
        model=fallback_model,
        temperature=kwargs.get("temperature", LLM_TEMPERATURE),
        top_p=kwargs.get("top_p", LLM_TOP_P),
        max_tokens=kwargs.get("max_tokens", LLM_MAX_TOKENS),
        max_retries=kwargs.get("max_retries", LLM_MAX_RETRIES),
    )
    try:
        if fallback_backend in ("openai", "openai_compatible"):
            fallback = _create_openai(base_url=fallback_base_url, **common)
        else:
            fallback = _create_ollama(base_url=fallback_base_url, **common)
    except Exception as e:
        if primary is not None:
            logger.warning(f"[failover] 备用后端创建失败，仅使用主后端: {e}")
            return primary
        raise RuntimeError(f"主备 LLM 后端均创建失败: primary={primary_error}, fallback={e}")

    failover = FailoverLLMBackend(primary=primary, fallback=fallback)
    if primary is None:
        failover.mark_degraded(f"主后端创建失败: {primary_error}")
    return failover


def _create_ollama(**kwargs) -> OllamaBackend:
    model = kwargs.get("model") or os.getenv("LLM_MODEL", "qwen2.5:7b")
    base_url = kwargs.get("base_url") or os.getenv("LLM_BASE_URL", "http://localhost:11434")
    temperature = kwargs.get("temperature", float(os.getenv("LLM_TEMPERATURE", "0.1")))
    top_p = kwargs.get("top_p", float(os.getenv("LLM_TOP_P", "0.9")))
    max_tokens = kwargs.get("max_tokens", int(os.getenv("LLM_MAX_TOKENS", "2048")))
    max_retries = kwargs.get("max_retries", int(os.getenv("LLM_MAX_RETRIES", "3")))
    # 显式设置 num_ctx 可覆盖模型声明窗口；默认 0 = 自动
    num_ctx = kwargs.get("num_ctx", int(os.getenv("OLLAMA_NUM_CTX", "0")))

    logger.info(f"创建 Ollama 后端: model={model}, base_url={base_url}")
    return OllamaBackend(
        model=model,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        max_retries=max_retries,
        num_ctx=num_ctx,
    )


def _create_openai(**kwargs) -> OpenAICompatibleBackend:
    model = kwargs.get("model") or os.getenv("OPENAI_MODEL", "deepseek-v4-flash")
    api_key = kwargs.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    base_url = kwargs.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    temperature = kwargs.get("temperature", float(os.getenv("LLM_TEMPERATURE", "0.1")))
    top_p = kwargs.get("top_p", float(os.getenv("LLM_TOP_P", "0.9")))
    max_tokens = kwargs.get("max_tokens", int(os.getenv("LLM_MAX_TOKENS", "2048")))
    max_retries = kwargs.get("max_retries", int(os.getenv("LLM_MAX_RETRIES", "3")))

    if not api_key:
        raise ValueError("使用 OpenAI 兼容后端必须设置 OPENAI_API_KEY 环境变量")

    safe_key = api_key[:8] + "..." if len(api_key) > 8 else "***"
    logger.info(f"创建 OpenAI 兼容后端: model={model}, base_url={base_url}, api_key={safe_key}")
    return OpenAICompatibleBackend(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )
