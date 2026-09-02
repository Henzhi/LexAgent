"""
工具注册表（M1 / F1）。

ToolRegistry 管理全部 Agent 可调用工具：
- register / get / list / has：注册表基本操作
- to_openai_schemas：输出 OpenAI 兼容 schema 列表（LLM tools 参数）
- execute：按名执行工具，统一捕获异常 → ToolResult(ok=False)，不抛出中断 ReAct 循环

共享约定（架构师 §8）：
- 工具执行失败不抛出，统一返回 ToolResult(ok=False)；
- summary 首词为错误类型标签：未知工具 / 参数校验失败 / 工具执行失败。

参数校验（2026-09-01 审查整改 B4）：
- 工具参数是 LLM 生成的，等同不可信输入。schema 的类型/枚举约束只在
  「发给模型」时生效还不够——执行前先经 LangChain 推导的 pydantic schema
  （tool_call_schema，与发给 LLM 的同一份）校验，再调 executor；
- 不直接用 `langchain_tool.invoke()`：BaseTool.run 会把非字符串返回值
  str() 化，而 executor 返回的是 ToolResult 数据类，结构化结果会被拍平；
  因此校验走 schema、执行仍走原 executor；
- 无 langchain_tool 的老式 ToolSpec 保持 executor 直调路径（不回归）。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

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
            if spec.langchain_tool is not None:
                result = self._invoke_validated(spec, arguments)
            else:
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
        except ValidationError as e:
            # B4：参数在执行前就被 pydantic 拦下（非法枚举/类型/缺必填）
            return ToolResult(
                tool=name,
                call_id=call_id,
                ok=False,
                summary=f"参数校验失败: {e.errors()[0].get('msg', e)}",
                data={},
            )
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

    @staticmethod
    def _invoke_validated(spec: ToolSpec, arguments: dict[str, Any]) -> ToolResult:
        """经 pydantic schema 校验后执行 executor（B4）。

        schema 取 `langchain_tool.tool_call_schema`（LLM 实际可传的参数集，
        已排除 injected 参数），与发给模型的约束是同一份；`model_validate`
        抛 ValidationError 由 execute() 统一归一化为「参数校验失败」。
        额外字段（LLM 幻觉参数）按 pydantic 默认策略忽略——白名单语义。
        """
        lc_tool = spec.langchain_tool
        schema = getattr(lc_tool, "tool_call_schema", None) or getattr(lc_tool, "args_schema", None)
        if schema is None:  # 无 schema 可校验时退回直调，不做静默拦截
            return spec.executor(**(arguments or {}))
        validated = schema.model_validate(dict(arguments or {}))
        return spec.executor(**validated.model_dump())
