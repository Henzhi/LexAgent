"""
工具注册表（M1 / F1）。

ToolRegistry 管理全部 Agent 可调用工具：
- register / get / list / has：注册表基本操作
- to_openai_schemas：输出 OpenAI 兼容 schema 列表（LLM tools 参数）
- execute：按名执行工具，统一捕获异常 → ToolResult(ok=False)，不抛出中断 ReAct 循环

共享约定（架构师 §8）：
- 工具执行失败不抛出，统一返回 ToolResult(ok=False)；
- summary 首词为错误类型标签：未知工具 / 参数校验失败 / 工具执行失败。
"""

from __future__ import annotations

import logging
from typing import Any

from src.agents.tools.base import ToolExecutionError, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册表（单例注册表由调用方持有，支持注册/列出/执行）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具。同名工具重复注册抛 ValueError（避免静默覆盖）。"""
        if spec is None or not spec.name:
            raise ValueError("工具必须有非空 name")
        if spec.name in self._tools:
            raise ValueError(f"工具已注册: {spec.name}")
        self._tools[spec.name] = spec
        logger.info(f"工具已注册: {spec.name} (category={spec.category})")

    def get(self, name: str) -> ToolSpec | None:
        """按名称获取工具，不存在返回 None。"""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolSpec]:
        """列出全部工具（按注册顺序）。"""
        return list(self._tools.values())

    def has(self, name: str) -> bool:
        """工具是否存在。"""
        return name in self._tools

    def to_openai_schemas(self) -> list[dict]:
        """输出 OpenAI 兼容 schema 列表，供 LLM chat_with_tools 使用。"""
        return [spec.to_openai_format() for spec in self._tools.values()]

    def langchain_tools(self) -> list:
        """LangChain BaseTool 列表（D-M3-13）——`chat_model.bind_tools()` 直接消费。

        与 `to_openai_schemas()` 内容等价，区别是传对象而非 dict，这是 LangChain
        的推荐写法（schema 由 BaseTool 自己负责，且能做参数校验）。
        工具未带 LangChain 对象时（如历史自定义 ToolSpec）跳过。
        """
        return [spec.langchain_tool for spec in self._tools.values() if spec.langchain_tool is not None]

    def execute(self, name: str, arguments: dict[str, Any], call_id: str = "") -> ToolResult:
        """执行工具。

        Args:
            name: 工具名
            arguments: 已解析的工具参数
            call_id: 对应 LLM tool_call 的 id（回填 tool 消息时使用）

        Returns:
            ToolResult：工具自身返回的结果；执行异常时归一化为 ok=False。
        """
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                summary=f"未知工具: {name}",
                data={},
            )
        if spec.executor is None:
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                summary="工具执行器未注册",
                data={},
            )
        try:
            result = spec.executor(**arguments)
            if not isinstance(result, ToolResult):
                # 防御：executor 返回非 ToolResult 时包装
                result = ToolResult(
                    tool=name,
                    call_id=call_id,
                    ok=True,
                    summary=str(result)[:300],
                    data={},
                )
            if call_id and not result.call_id:
                result.call_id = call_id
            if not result.tool:
                result.tool = name
            return result
        except ToolExecutionError as e:
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                summary=f"工具执行失败: {e}",
                data={},
            )
        except TypeError as e:
            # 参数与 executor 签名不匹配（LLM 传参错误）
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                summary=f"参数校验失败: {e}",
                data={},
            )
        except Exception as e:
            logger.error(f"工具执行异常: {name} args={arguments}", exc_info=True)
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                summary=f"工具执行失败: {e}",
                data={},
            )
