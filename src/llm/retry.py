"""
统一 LLM / Embedding 调用重试策略。

解决的问题：
- 旧实现 `except Exception` 捕获一切异常并重试：4xx 业务错误（如 400 参数、
  401 鉴权）重试必然失败，白白消耗请求；429 限流用固定线性退避且无抖动，
  多请求同时失败会同时重试，形成惊群，越重试越被限流。
- 流式请求在"已产出部分内容"时失败仍重试整个流，导致用户看到重复内容，
  且重复计费。

本模块提供三件事：
1. `is_retryable(exc)`：判断异常是否值得重试（429 / 5xx / 网络 / 超时可重试；
   4xx 业务错误、鉴权失败不可重试）。
2. `get_retry_after_seconds(exc)`：优先读取供应商返回的 Retry-After 头。
3. `backoff_delay(...)`：指数退避 + 全抖动（jitter），避免惊群。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any

logger = logging.getLogger(__name__)

# 不重试的状态码：重试必然失败或属业务错误
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 405, 409, 413, 422}


def _status_code_from(exc: BaseException) -> int | None:
    """尽量从异常对象中提取 HTTP 状态码（兼容 openai / ollama / requests / httpx）。"""
    # openai SDK: RateLimitError / APIStatusError 等带 status_code
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc

    # ollama SDK: ResponseError.status_code
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc

    # requests / httpx 包装的 HTTP 错误：从响应对象上取
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if isinstance(sc, int):
            return sc

    # openai APIStatusError 的 response 也可取 headers
    return None


def _is_network_error(exc: BaseException) -> bool:
    """网络类错误（连接失败 / 超时 / 服务端中断）通常可重试。"""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "connecterror", "network")):
        return True


    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        # OSError 可能是磁盘/权限等，但连接相关大概率可重试；保守放行
        return True

    # openai APIConnectionError / APITimeoutError
    for base in type(exc).__mro__:
        if base.__name__ in ("APIConnectionError", "APITimeoutError", "ConnectionError", "TimeoutError"):
            return True
    return False


def is_retryable(exc: BaseException) -> bool:
    """判断该异常是否值得重试。

    可重试:
      - 429（限流）、5xx（服务端故障）
      - 网络连接失败 / 超时 / 服务端流中断
    不可重试:
      - 4xx 业务错误（参数、鉴权、不存在等）
      - 非网络类编程错误（不重试，避免掩盖 bug）
    """
    status = _status_code_from(exc)
    if status is not None:
        if status == 429 or status >= 500:
            return True
        if status in _NON_RETRYABLE_STATUS:
            return False
        # 其他 4xx（如 408 Request Timeout）归为可重试的边界情况
        return status == 408

    if _is_network_error(exc):
        return True

    # 兜底：显式标注可重试的异常类型（如自定义 RateLimitedError）
    return type(exc).__name__ in (
        "RateLimitError", "RateLimitedError", "TooManyRequests",
        "APIConnectionError", "APITimeoutError", "InternalServerError",
    )


def get_retry_after_seconds(exc: BaseException) -> float | None:
    """从异常中读取供应商返回的 Retry-After 头（秒数）。

    返回 None 表示没有该头，由调用方使用默认退避策略。
    """
    for attr in ("headers",):
        headers = getattr(exc, attr, None) or {}
        if isinstance(headers, dict):
            val = headers.get("Retry-After") or headers.get("retry-after")
            if val is not None:
                return _parse_retry_after(val)

    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("headers",):
            headers = getattr(resp, attr, None)
            if headers is None:
                continue
            if isinstance(headers, dict):
                val = headers.get("Retry-After") or headers.get("retry-after")
            else:
                val = headers.get("Retry-After")
            if val is not None:
                return _parse_retry_after(val)
    return None


def _parse_retry_after(val: Any) -> float | None:
    try:
        # HTTP-date 形式（RFC 7231）：先尝试秒数，再尝试日期
        seconds = float(val)
        return max(0.0, seconds)
    except (TypeError, ValueError):
        pass
    try:
        # 例如 "Wed, 21 Oct 2015 07:28:00 GMT"
        parsed = time.mktime(time.strptime(val, "%a, %d %b %Y %H:%M:%S %Z"))
        return max(0.0, parsed - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """指数退避 + 全抖动。

    Args:
        attempt: 第几次失败（从 1 开始）
        base: 基础间隔（秒）
        cap: 最大间隔上限（秒）

    Returns:
        建议等待秒数。第 n 次失败 → 均匀分布在 [0, min(base * 2**(n-1), cap)]
    """
    max_delay = min(base * (2 ** (attempt - 1)), cap)
    # 全抖动：均匀随机 [0, max_delay]，防止多请求同时重试
    return random.uniform(0, max_delay)


def wait_and_log(exc: BaseException, attempt: int, max_retries: int, logger_name: str = __name__) -> None:
    """按策略等待后写日志。仅当还有剩余重试次数时调用。"""
    if attempt >= max_retries:
        return
    retry_after = get_retry_after_seconds(exc)
    if retry_after is not None:
        delay = retry_after
    else:
        delay = backoff_delay(attempt, base=1.0, cap=30.0)
    log = logging.getLogger(logger_name)
    log.warning(
        "调用失败 (尝试 %d/%d)，%.1fs 后重试: %s",
        attempt, max_retries, delay, exc,
    )
    time.sleep(delay)
