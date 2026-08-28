"""
工具自描述与执行结果的基础数据结构（M1 / F1）。

- ToolSpec：工具自描述（name/description/JSON-Schema/executor），`to_openai_format()`
  输出 OpenAI 兼容 schema，直接用于 DeepSeek / Ollama 的 `tools` 参数。
- ToolResult：统一工具执行结果。`source` 取值 internal_kb | web（legal_source 为 M2 预留）。
- ToolExecutionError：工具执行期内部错误（工具层内部使用；工具失败统一返回
  ToolResult(ok=False)，不抛出中断 ReAct 循环）。

共享约定（架构师 §8）：
- ToolResult.summary 截断到 TOOL_RESULT_SUMMARY_MAX_CHARS（默认 300）字符，
  供 SSE tool_result 展示与 LLM 回灌，控制上下文膨胀。
- 失败时 summary 首词为错误类型标签（如 搜索不可用、参数校验失败、工具执行失败）。
"""
from __future__ import annotations

import inspect
import logging
import types
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Union, get_args, get_origin, get_type_hints

from src.config import TOOL_RESULT_SUMMARY_MAX_CHARS

logger = logging.getLogger(__name__)

# 工具结果来源标记（D7 / M2 预留）
SOURCE_INTERNAL_KB = "internal_kb"  # 内部法律知识库
SOURCE_WEB = "web"                  # 网络搜索（Tavily）
SOURCE_LEGAL = "legal_source"       # 官方法律源（M2 / F9）

# 工具分类
CATEGORY_KNOWLEDGE = "knowledge"    # 内部知识检索
CATEGORY_WEB = "web"                # 网络搜索
CATEGORY_LEGAL = "legal"            # 官方法律源检索（M2）


def truncate_summary(text: str, max_chars: int = TOOL_RESULT_SUMMARY_MAX_CHARS) -> str:
    """截断摘要到指定字符数，保证 SSE 事件体不膨胀。"""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


class ToolExecutionError(Exception):
    """工具执行内部错误（注册表内部使用，不对外抛出）。"""


@dataclass
class ToolSpec:
    """工具自描述。

    Attributes:
        name: 工具名（LLM 决策调用时使用，须唯一）
        description: 工具用途描述（LLM 路由决策依据）
        parameters: JSON Schema 的 properties（{参数名: {"type":..., "description":...}}）
        required: 必填参数名列表
        category: 工具分类（knowledge | web）
        executor: 执行函数，签名与 parameters 对齐，返回 ToolResult
    """

    name: str
    description: str
    parameters: dict[str, dict[str, Any]]
    required: list[str] = field(default_factory=list)
    category: str = CATEGORY_KNOWLEDGE
    executor: Callable[..., "ToolResult"] | None = None

    def to_openai_format(self) -> dict:
        """输出 OpenAI 兼容的工具 schema（tools 参数直接使用）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }


@dataclass
class ToolResult:
    """统一工具执行结果。

    Attributes:
        tool: 工具名
        call_id: 对应 LLM tool_call 的 id（回填 tool 消息时使用）
        ok: 是否成功
        summary: 截断摘要（≤300 字符），供 SSE 展示与 LLM 回灌
        data: 结构化结果（如检索 docs 列表），供 M2 融合与可观测性复用
        source: 来源标记（internal_kb | web）
    """

    tool: str
    call_id: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_INTERNAL_KB

    def __post_init__(self) -> None:
        self.summary = truncate_summary(self.summary)

    def to_tool_message(self) -> dict:
        """构造 tool 角色消息（回灌 LLM 的通用格式）。"""
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": self.summary,
        }

    def to_log_entry(self) -> dict:
        """转换为可观测性日志条目（写入 state.tool_log）。"""
        return {
            "tool": self.tool,
            "ok": self.ok,
            "summary": self.summary,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# @tool 装饰器：函数式工具声明（M3，零第三方依赖）
#
# 只做语法糖——从类型注解 + Annotated 元数据推导 OpenAI JSON Schema，
# 产出与手写 class 完全一致的 ToolSpec。不引入 LangChain 的 @tool /
# BaseChatModel / bind_tools（D1 决策已否决那条路线）。
#
# 用法:
#     def build_xxx_spec(dep) -> ToolSpec:
#         @tool(name="xxx", category=CATEGORY_WEB)
#         def xxx(query: Annotated[str, "检索关键词"],
#                 top_k: Annotated[int, "返回条数"] = 5) -> ToolResult:
#             '''工具描述（docstring 即 description）。'''
#             return ToolResult(tool="xxx", call_id="", ok=True, summary="...")
#         return xxx
# ---------------------------------------------------------------------------

# Python 类型 → JSON Schema type
_PY_TO_JSON_TYPE: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass(frozen=True)
class Param:
    """工具参数的额外描述（用于 `Annotated[T, Param(...)]`）。

    简写：只给描述时可直接写字符串——`Annotated[str, "描述文本"]`。

    Attributes:
        description: 参数说明（进入 schema，供 LLM 理解参数含义）
        enum: 可选值枚举（进入 schema 的 enum，约束 LLM 输出）
    """

    description: str = ""
    enum: list[Any] | None = None


def _json_type_of(annotation: Any) -> str:
    """Python 类型注解 → JSON Schema type 字符串。

    `Optional[X]` / `X | None` 取 X 的类型（可选性由 required 表达，不由 type 表达）；
    无法识别的类型一律降级为 string（宁可宽松，也不让 schema 校验卡死 LLM）。
    """
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _json_type_of(non_none[0])
    return _PY_TO_JSON_TYPE.get(annotation, "string")


def _build_parameters(fn: Callable) -> tuple[dict[str, dict], list[str]]:
    """从函数签名推导 (parameters, required)。

    处理 `from __future__ import annotations` 下的字符串注解：必须走
    `get_type_hints(..., include_extras=True)` 才能拿到 Annotated 元数据。
    """
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        # 解析失败（如闭包引用的局部类型）时退化为原始 __annotations__
        hints = dict(getattr(fn, "__annotations__", {}) or {})

    parameters: dict[str, dict] = {}
    required: list[str] = []

    for pname, p in inspect.signature(fn).parameters.items():
        if pname in ("self", "cls"):
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue

        hint = hints.get(pname, str)
        description = ""
        enum: list[Any] | None = None

        if get_origin(hint) is Annotated:
            meta_args = get_args(hint)
            hint = meta_args[0]
            for m in meta_args[1:]:
                if isinstance(m, Param):
                    description, enum = m.description, m.enum
                elif isinstance(m, str):
                    description = m  # 简写：直接给描述字符串

        schema: dict[str, Any] = {"type": _json_type_of(hint)}
        if enum:
            schema["enum"] = list(enum)
        if description:
            schema["description"] = description
        parameters[pname] = schema

        if p.default is inspect.Parameter.empty:
            required.append(pname)

    return parameters, required


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    category: str = CATEGORY_KNOWLEDGE,
) -> Callable[[Callable[..., ToolResult]], ToolSpec]:
    """把函数声明为 Agent 工具，产出 ToolSpec（供 ToolRegistry 注册）。

    Args:
        name: 工具名，缺省用函数名
        description: 工具描述，缺省用函数 docstring（推荐写在 docstring）
        category: 工具分类（CATEGORY_KNOWLEDGE / CATEGORY_WEB / CATEGORY_LEGAL）

    Returns:
        装饰器：接收工具函数，返回 ToolSpec（函数本身即 executor）。

    Raises:
        ValueError: 函数无任何参数且也未声明 name（防御性，实际不会触发）
    """

    def decorator(fn: Callable[..., ToolResult]) -> ToolSpec:
        tool_name = name or fn.__name__
        parameters, required = _build_parameters(fn)
        return ToolSpec(
            name=tool_name,
            description=(description or inspect.getdoc(fn) or "").strip() or tool_name,
            parameters=parameters,
            required=required,
            category=category,
            executor=fn,
        )

    return decorator
