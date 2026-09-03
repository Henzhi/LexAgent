"""
F15 用量采集 callback（旁路 token 计费埋点，M4 / F15）。

**为什么独立于 LLMBudgetCallbackHandler**：预算 handler 的 `on_llm_end` 语义是
「不再计数」（配额已在 `on_llm_start` 预占，再记会把日限额腰斩）——那是**次数**
口径。本 handler 采集 **token** 口径，两者关注点不同，混在一起职责冲突且难测。

**采集策略**（对照 docs/F15-日志与Token计费面板-技术方案.md）：
- `on_llm_end` 读 `response.usage_metadata`：`input_tokens` / `output_tokens`；
- DeepSeek 缓存命中价比未命中便宜 50 倍，必须拆 `prompt_cache_hit_tokens` /
  `prompt_cache_miss_tokens`——拆不到（SDK 版本差异）则全部按未命中计并标 est
  （保守，宁可高估不低估）；
- 流式 / Ollama 拿不到 usage_metadata → 用 `on_llm_start` 缓存的 prompt 文本 +
  输出文本 tiktoken 估算，标 est；
- **只记成功调用**（on_llm_end 触发）；失败（on_llm_error）不记——与 F14
  「请求未真正完成不占配额」一致；
- 统计组件故障一律告警放行，不拖垮主链路（D-M3-8 原则）。

用法（OpenAI / Ollama 两个后端构造 ChatModel 时挂载）:

    ChatOpenAI(..., callbacks=[*budget_callbacks(), *usage_callbacks(backend="deepseek")])
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

# 每次调用缓存 on_llm_start 的 prompt 文本上限（超限丢弃最旧，防止内存膨胀）
_MAX_START_CACHE = 256


class LLMUsageCallbackHandler(BaseCallbackHandler):
    """LLM token 用量采集（F15）：成功调用后把 usage 落 usage_logs。

    Args:
        backend: 后端标识（deepseek / ollama），决定计价分支
        model: 模型名（deepseek-v4-flash / qwen2.5:7b ...）
        request_id / session_id / user_id: 关联上下文（可由外层注入）
    """

    def __init__(
        self,
        backend: str = "deepseek",
        model: str = "",
        *,
        user_id: str = "default",
        request_id: str | None = None,
        session_id: str | None = None,
    ):
        self.backend = str(backend or "").lower()
        self.model = str(model or self.backend)
        self.user_id = user_id
        self.request_id = request_id
        self.session_id = session_id
        # run_id → prompt 文本（流式/无 usage 时估算输入 token 用）
        self._start_prompts: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "llm_usage_callback"

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """缓存本次调用 prompt（供无 usage 时估算输入 token）。

        LangChain 一次调用可能触发多次 on_llm_start？不会——handler 挂在单个
        ChatModel 上，每次 invoke/stream 触发一次。run_id 取 kwargs 里的 run_id。
        """
        run_id = str(kwargs.get("run_id") or "")
        if not run_id or not prompts:
            return
        joined = "\n".join(str(p) for p in prompts)
        with self._lock:
            if len(self._start_prompts) >= _MAX_START_CACHE:
                self._start_prompts.pop(next(iter(self._start_prompts)))
            self._start_prompts[run_id] = joined

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """调用成功：读 usage 落库。任何异常都不上抛——观测故障不拖垮主链路。"""
        run_id = str(kwargs.get("run_id") or "")
        prompt_text = self._pop_prompt(run_id)
        try:
            usage = self._extract_usage(response)
            if usage is not None:
                prompt_tokens, completion_tokens, cache_hit, cache_miss = usage
                est = False
            else:
                # 无 usage（流式未开 / Ollama）：按文本估算，标 est
                completion_text = self._extract_completion_text(response)
                prompt_tokens, completion_tokens, cache_hit, cache_miss = self._estimate(
                    prompt_text or "", completion_text or ""
                )
                est = True
            self._record(prompt_tokens, completion_tokens, cache_hit, cache_miss, est)
        except Exception as e:
            logger.warning(f"LLM usage 采集失败（忽略）: {e}")

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """调用失败：清理缓存，不记录（与 F14「未真正完成不占配额」一致）。"""
        run_id = str(kwargs.get("run_id") or "")
        if run_id:
            with self._lock:
                self._start_prompts.pop(run_id, None)

    # ------------------------------------------------------------------
    # 解析与估算
    # ------------------------------------------------------------------

    def _pop_prompt(self, run_id: str) -> str:
        if not run_id:
            return ""
        with self._lock:
            return self._start_prompts.pop(run_id, "") or ""

    def _extract_usage(self, response: Any) -> tuple[int, int, int, int] | None:
        """从 LangChain 响应里拆 usage → (prompt, completion, cache_hit, cache_miss)。

        三级降级解析 cache 拆分（DeepSeek 价差 50 倍，拆不到按全 miss 保守计）：
        1. usage_metadata.input_token_details.cache_read（LangChain 归一字段）；
        2. response_metadata / usage 里的 prompt_cache_hit_tokens；
        3. 都没有 → cache_hit=0，输入全算未命中（返回 miss 缺省由计价器兜底）。
        """
        md = getattr(response, "usage_metadata", None) or {}
        if not md:
            # 兼容 LLMResult 形态（老版本 callback 传 LLMResult 而非 message）
            llm_result = response
            gens = getattr(llm_result, "generations", None)
            if gens and gens[0]:
                msg = getattr(gens[0][0], "message", None)
                if msg is not None:
                    md = getattr(msg, "usage_metadata", None) or {}
        if not md:
            return None
        prompt = int(md.get("input_tokens") or 0)
        completion = int(md.get("output_tokens") or 0)
        cache_hit = 0
        details = md.get("input_token_details") or {}
        if isinstance(details, dict) and details.get("cache_read"):
            cache_hit = int(details["cache_read"])
        if cache_hit == 0:
            # 二级：从原始 usage（response_metadata）读 DeepSeek 专有字段
            resp_md = getattr(response, "response_metadata", None) or {}
            raw = resp_md.get("usage") or {}
            cache_hit = int(raw.get("prompt_cache_hit_tokens") or 0)
        if cache_hit > prompt:
            cache_hit = prompt
        return prompt, completion, cache_hit, max(0, prompt - cache_hit)

    def _estimate(self, prompt_text: str, completion_text: str) -> tuple[int, int, int, int]:
        """tiktoken 估算（无 usage 时的兜底）。import 失败则全 0（放弃估算）。"""
        try:
            from src.memory.token_budget import TokenBudget

            prompt = TokenBudget.count(prompt_text) if prompt_text else 0
            completion = TokenBudget.count(completion_text) if completion_text else 0
        except Exception as e:  # pragma: no cover - tiktoken 编码加载失败属环境问题
            logger.debug(f"tiktoken 估算失败（token 记 0）: {e}")
            return 0, 0, 0, 0
        return prompt, completion, 0, prompt  # 估算无法区分缓存 → 全按未命中

    @staticmethod
    def _extract_completion_text(response: Any) -> str:
        text = getattr(response, "content", None)
        if isinstance(text, str):
            return text
        if isinstance(text, list):
            parts = []
            for block in text:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("content") or ""))
                else:
                    parts.append(str(block))
            return "".join(parts)
        return ""

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _record(self, prompt_tokens: int, completion_tokens: int, cache_hit: int, cache_miss: int, est: bool) -> None:
        from src.observability.usage_store import record_llm_usage

        record_llm_usage(
            model=self.model,
            backend=self.backend,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
            est=est,
            user_id=self.user_id,
            request_id=self.request_id,
            session_id=self.session_id,
        )


def usage_callbacks(
    backend: str = "deepseek",
    model: str = "",
    **ctx: Any,
) -> list[BaseCallbackHandler]:
    """构造 ChatModel 时挂载的 usage 采集 callback 列表。

    与 `budget_callbacks()` 并列使用：`callbacks=[*budget_callbacks(), *usage_callbacks(...)]`。
    模型名缺省由各后端构造时传入（此时 self.model 已就绪）。
    """
    return [LLMUsageCallbackHandler(backend=backend, model=model, **ctx)]
