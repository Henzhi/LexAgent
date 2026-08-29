"""
LLM 后端抽象基类。

定义统一的 LLM 调用接口，支持 Ollama 和 OpenAI 兼容 API 两种后端。
所有后端实现必须继承此基类并实现对应方法。
M1 新增：ToolCall / ToolCallResponse 数据结构 + chat_with_tools()（F2 工具调用）。
D-M3-13：内部实现改为 LangChain 的 BaseChatModel，预算埋点移到
`src/llm/budget_callback.py`（原 _budget_check / _budget_record 已删除）。
"""
from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def parse_tool_arguments(args_raw: str) -> tuple[dict[str, Any], str]:
    """解析 LLM 返回的 function.arguments JSON 字符串。

    Args:
        args_raw: function.arguments 原文（通常是 JSON 字符串）

    Returns:
        (arguments, error)：解析成功时 error 为空；失败时返回 ({}, 错误描述)。
        容错：先尝试完整 JSON 解析；失败时尝试提取第一个 {...} 片段（模型可能夹带说明文字）。
    """
    if not args_raw or not str(args_raw).strip():
        return {}, ""
    text = str(args_raw).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, ""
        return {}, "参数不是 JSON 对象"
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed, ""
        except (ValueError, TypeError):
            pass
    return {}, f"参数 JSON 解析失败: {text[:80]}"


@dataclass
class ToolCall:
    """一次 LLM 工具调用请求。

    Attributes:
        id: 工具调用 ID（回填 tool 消息时对应 tool_call_id）
        name: 工具名；空 name 一律视为无效占位（如 DeepSeek V4 想直接回答时
              返回的函数名为空的 tool_call），解析层与 agent 节点均应跳过。
        arguments: 已解析的 JSON 参数
        parse_error: arguments JSON 解析失败时的错误信息（空 = 解析成功）。
                     解析失败时 tools 节点不回灌错误消息前不执行工具（R1 容错）。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str = ""

    def to_message(self) -> dict:
        """构造 assistant tool_calls 消息（OpenAI/DeepSeek/Ollama 兼容形态）。"""
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": self.id,
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "arguments": json.dumps(self.arguments, ensure_ascii=False),
                    },
                }
            ],
        }


@dataclass
class ToolCallResponse:
    """一次带工具调用的 LLM 响应。

    Attributes:
        content: 最终答案文本（决策轮通常为空；tool_calls 为空时即最终答案）
        tool_calls: 工具调用列表（空列表 = 无工具调用，content 即最终答案）
        raw: 原始响应（调试/可观测）
    """

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LangChain 互操作（D-M3-13：LLM 层迁移到 LangChain 标准生态）
#
# 项目内部消息格式一直是 OpenAI 风格的 dict（{"role", "content", ...}），
# 迁移时保留这个内部表示，只在实际调用 LangChain 时做一次转换——
# 上层 18 处调用点与 state 里的消息历史都不受影响。
# ---------------------------------------------------------------------------


def to_langchain_messages(messages: list[dict]) -> list[Any]:
    """项目内部的 OpenAI 格式 dict 消息 → LangChain Message 对象。

    支持 system / user / assistant / assistant(tool_calls) / tool 五种形态，
    与 react_nodes 回灌的消息结构一致（共享约定 §8.4）。
    """
    from langchain_core.messages import convert_to_messages

    return convert_to_messages(messages or [])


def tool_calls_from_langchain(message: Any) -> list[ToolCall]:
    """LangChain AIMessage.tool_calls → 项目 ToolCall 列表。

    空 name 的占位 tool_call 一律跳过（D-M1-6）：DeepSeek V4 的
    parallel_tool_calls 恒启用，模型想直接回答时会返回函数名为空的占位调用。

    注意：LangChain 的 tool_call 里参数是**已解析的 dict**（`args`），
    不像 OpenAI 原始响应那样是 JSON 字符串，因此不存在解析失败的情况，
    parse_error 恒为空。
    """
    result: list[ToolCall] = []
    for tc in getattr(message, "tool_calls", None) or []:
        if isinstance(tc, dict):
            name = tc.get("name") or ""
            args = tc.get("args") or {}
            call_id = tc.get("id") or ""
        else:
            name = getattr(tc, "name", "") or ""
            args = getattr(tc, "args", None) or {}
            call_id = getattr(tc, "id", "") or ""
        if not name:
            continue
        result.append(ToolCall(id=call_id, name=name, arguments=args))
    return result


class LLMBackend(ABC):
    """LLM 后端抽象基类

    子类需实现:
      - _generate_impl(): 同步生成
      - _stream_impl(): 流式生成
      - context_window: 返回上下文窗口大小

    D-M3-13：内部实现改为 LangChain 的 `BaseChatModel`（ChatOpenAI / ChatOllama），
    并通过 `.model` 属性暴露，上层可直接用标准写法：

        llm.model.bind_tools(schemas).invoke(messages)

    对外的 `chat` / `chat_stream` / `chat_with_tools` 三个入口保持不变，
    供上层 18 处既有调用点继续使用，迁移期零改动。
    """

    def __init__(
        self,
        model: str,
        temperature: float = 0.1,
        top_p: float = 0.9,
        max_tokens: int = 2048,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        # 子类在 __init__ 中赋值为具体的 BaseChatModel
        self._model: Any = None

    # ------------------------------------------------------------------
    # LangChain 标准入口（D-M3-13）
    # ------------------------------------------------------------------

    @property
    def chat_model(self) -> Any:
        """底层 LangChain ChatModel（`BaseChatModel`）。

        注：不叫 `.model` 是因为该名字已被模型名字符串占用（历史包袱，
        18 处调用点与多处配置都在读 `llm.model` 取模型名）。
        """
        if self._model is None:
            raise NotImplementedError(
                f"{type(self).__name__} 未初始化 LangChain ChatModel"
            )
        return self._model

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def chat(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """单轮对话（同步）

        Args:
            user_message: 用户消息
            history: 历史对话 [{"role": "...", "content": "..."}, ...]
            system_prompt: 系统提示词

        Returns:
            LLM 响应文本

        Raises:
            BudgetExceededError: 当日 LLM 调用预算已用尽（F14）

        注（D-M3-13）：预算的检查与计数不再写在这里，改由
        `LLMBudgetCallbackHandler` 在 LangChain 调用链路上统一埋点——
        这样无论走本入口还是直接 `chat_model.invoke()` 都会被计数。
        """
        messages = self._build_messages(user_message, history, system_prompt)
        return self._generate_impl(messages)

    def chat_stream(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> Iterator[str]:
        """流式对话

        Yields:
            逐个 token 的输出文本

        Raises:
            BudgetExceededError: 当日 LLM 调用预算已用尽（F14）

        注（D-M3-13）：预算的检查与计数不再写在这里，改由
        `LLMBudgetCallbackHandler` 在 LangChain 调用链路上统一埋点——
        这样无论走本入口还是直接 `chat_model.invoke()` 都会被计数。
        """
        messages = self._build_messages(user_message, history, system_prompt)
        yield from self._stream_impl(messages)

    # ------------------------------------------------------------------
    # 工具调用（M1 / F2）
    # ------------------------------------------------------------------

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        """一次 LLM 调用：允许模型返回 tool_calls 或最终文本。

        Args:
            messages: 完整消息列表（含 system/history/工具回灌消息）
            tools: OpenAI 兼容的工具 schema 列表（ToolRegistry.to_openai_schemas()）
            tool_choice: 工具选择策略（auto/required/指定工具），默认 auto

        Returns:
            ToolCallResponse：tool_calls 非空表示模型请求调用工具；
            为空时 content 即最终答案文本。

        Raises:
            BudgetExceededError: 当日 LLM 调用预算已用尽（F14）

        注（D-M3-13）：预算的检查与计数不再写在这里，改由
        `LLMBudgetCallbackHandler` 在 LangChain 调用链路上统一埋点——
        这样无论走本入口还是直接 `chat_model.invoke()` 都会被计数。
        """
        return self._chat_with_tools_impl(messages, tools, tool_choice)

    def _chat_with_tools_impl(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        """工具调用实现（默认退化为普通生成，返回空 tool_calls）。

        子类（OpenAI / Ollama）应覆盖以实现真正的工具调用；
        不支持的模型自然返回空 tool_calls，上层据此走固定管线/直接生成。
        """
        content = self._generate_impl(messages)
        return ToolCallResponse(content=content, tool_calls=[], raw={})

    # ------------------------------------------------------------------
    # 抽象方法（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def _generate_impl(self, messages: list[dict[str, str]]) -> str:
        """同步生成实现"""
        ...

    @abstractmethod
    def _stream_impl(self, messages: list[dict[str, str]]) -> Iterator[str]:
        """流式生成实现"""
        ...

    @abstractmethod
    def get_context_window(self) -> int:
        """返回该模型的有效上下文窗口大小 (tokens)"""
        ...

    # ------------------------------------------------------------------
    # 公共工具方法
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> list[dict[str, str]]:
        """构建标准消息列表"""
        messages: list[dict[str, str]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if history:
            for msg in history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("system", "user", "assistant"):
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})
        return messages

    @staticmethod
    def _build_rag_prompt(query: str, context: str) -> str:
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
