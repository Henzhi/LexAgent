"""
@tool 装饰器测试（M3 + D-M3-13）。

D-M3-13 后 schema 推导交给 LangChain 的 `@tool`，本装饰器只负责再包一层
`ToolSpec`（带项目自己的 category / executor / langchain_tool）。

与迁移前的行为差异（已实测确认，见各用例注释）：
1. `str | None` → `anyOf [string, null]`（原为扁平 string），语义等价且更规范
2. dataclass 类型 → 展开为对象 schema（原降级为 string）
3. 枚举必须用 `Literal` 表达；历史上的 `Param` 类会被 LangChain **静默丢弃**
"""

from __future__ import annotations

from typing import Annotated, Literal

from src.rag.retriever import RetrievedDoc
from src.agents.tools.base import (
    CATEGORY_LEGAL,
    ToolResult,
    ToolSpec,
    tool,
)


class TestToolDecoratorBasics:
    def test_returns_toolspec_with_executor(self):
        """装饰器产出 ToolSpec，executor 即原函数。"""

        @tool(name="my_tool", category=CATEGORY_LEGAL)
        def my_tool(query: Annotated[str, "查询"]) -> ToolResult:
            """演示工具。"""
            return ToolResult(tool="my_tool", call_id="", ok=True, summary="ok")

        assert isinstance(my_tool, ToolSpec)
        assert my_tool.name == "my_tool"
        assert my_tool.category == CATEGORY_LEGAL
        assert my_tool.executor(query="x").ok is True

    def test_name_defaults_to_function_name(self):
        @tool()
        def auto_named(query: str) -> ToolResult:
            """自动命名。"""
            return ToolResult(tool="", call_id="", ok=True, summary="")

        assert auto_named.name == "auto_named"

    def test_docstring_becomes_description(self):
        """docstring 整体作为 description（缩进已清理）。

        不做"只取第一段"的截断——工具描述是给 LLM 的路由依据，
        多句说明（何时用、注意事项）信息量越高越好。
        """

        @tool(name="t")
        def t(query: str) -> ToolResult:
            """第一行描述。

            第二段落，说明使用场景。
            """
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        assert t.description == "第一行描述。\n\n第二段落，说明使用场景。"

    def test_explicit_description_overrides_docstring(self):
        @tool(name="t", description="显式描述")
        def t(query: str) -> ToolResult:
            """docstring 描述。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        assert t.description == "显式描述"


class TestSchemaDerivation:
    def test_types_and_required(self):
        """类型映射 + required 由默认值有无决定。"""

        @tool(name="t")
        def t(
            query: Annotated[str, "关键词"],
            top_k: Annotated[int, "条数"] = 5,
            ratio: float = 0.5,
            flag: bool = False,
        ) -> ToolResult:
            """类型映射测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["query"] == {"type": "string", "description": "关键词"}
        # pydantic 会把默认值写进 schema（迁移前自研推导不写）。
        # 这是有益的差异：模型能感知默认值，不传时心里有数。
        assert props["top_k"]["type"] == "integer"
        assert props["top_k"]["description"] == "条数"
        assert props["top_k"]["default"] == 5
        assert props["ratio"]["type"] == "number"
        assert props["flag"]["type"] == "boolean"
        assert t.required == ["query"]

    def test_optional_type_yields_anyof(self):
        """`str | None` → anyOf [string, null]（pydantic 的标准表达）。

        迁移前自研推导会展开成扁平的 string；迁移后由 pydantic 生成 anyOf。
        语义等价，且更符合 JSON Schema 规范。
        """

        @tool(name="t")
        def t(query: str, doc_type: str | None = None) -> ToolResult:
            """Optional 测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["doc_type"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
        assert t.required == ["query"]

    def test_literal_enum_and_description(self):
        """枚举约束用 `Literal` 表达（LangChain 原生写法）。

        ⚠️ 历史上用 `Annotated[str, Param(...)]`，但 LangChain 不认识 `Param`，
        会**静默丢弃**其中的 description 与 enum（实测确认）——不报错，只是
        发给模型的 schema 少了引导信息，很难发现。改用 Literal 后都正常。
        """

        @tool(name="t")
        def t(
            query: str,
            source_type: Annotated[Literal["law", "case", "all"], "来源类型"] = "law",
        ) -> ToolResult:
            """enum 测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["source_type"]["enum"] == ["law", "case", "all"]
        assert props["source_type"]["description"] == "来源类型"

    def test_dataclass_type_is_expanded(self):
        """dataclass 类型 → 展开为对象 schema（迁移前是降级为 string）。

        实际工具参数不会用这种复杂类型，此例仅记录行为差异。

        注：import 必须在模块顶层——LangChain 用 get_type_hints 解析注解，
        函数内 import 的名字它取不到（NameError）。
        """

        @tool(name="t")
        def t(doc: RetrievedDoc) -> ToolResult:  # type: ignore[valid-type]
            """dataclass 类型测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["doc"]["type"] == "object"
        assert "content" in props["doc"]["properties"]

    def test_openai_format_shape(self):
        """产出结构对齐 OpenAI tools 参数格式。"""

        @tool(name="t", category=CATEGORY_LEGAL)
        def t(query: Annotated[str, "q"]) -> ToolResult:
            """格式测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        schema = t.to_openai_format()
        assert schema["type"] == "function"
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert params["required"] == ["query"]


class TestRealToolsUseDecorator:
    """真实工具的 schema 与行为不变（改造前后一致性）。"""

    def test_retrieve_knowledge_schema(self):
        from src.agents.tools.retrieve_knowledge import build_retrieve_knowledge_spec
        from tests.fakes import FakeRetriever

        spec = build_retrieve_knowledge_spec(FakeRetriever())
        schema = spec.to_openai_format()["function"]
        assert schema["name"] == "retrieve_knowledge"
        props = schema["parameters"]["properties"]
        assert set(props) == {"query", "doc_type", "top_k"}
        # doc_type 是 Literal|None → enum 包在 anyOf 里
        assert props["doc_type"]["anyOf"][0]["enum"] == ["law", "case"]
        assert schema["parameters"]["required"] == ["query"]

    def test_web_search_schema(self):
        from unittest.mock import MagicMock

        from src.agents.tools.web_search import build_web_search_spec

        spec = build_web_search_spec(MagicMock())
        schema = spec.to_openai_format()["function"]
        assert schema["name"] == "web_search"
        assert set(schema["parameters"]["properties"]) == {"query", "max_results"}
        assert schema["parameters"]["required"] == ["query"]

    def test_legal_source_search_schema(self):
        from unittest.mock import MagicMock

        from src.agents.tools.legal_source_search import build_legal_source_search_spec

        spec = build_legal_source_search_spec(MagicMock())
        schema = spec.to_openai_format()["function"]
        assert schema["name"] == "legal_source_search"
        props = schema["parameters"]["properties"]
        assert props["source_type"]["enum"] == ["law", "case", "all"]
        assert schema["parameters"]["required"] == ["query"]

    def test_default_tools_still_register_three(self, monkeypatch):
        """build_default_tools 注册数量与名称不变（集成回归）。

        注：北大法宝 MCP 接入后默认工具集扩展为 5 个（新增 pkulaw_search /
        pkulaw_verify），故此处隔离 PKULAW_ENABLED，聚焦原有三件套。
        """
        from src.agents.tools import build_default_tools
        from tests.fakes import FakeRetriever

        monkeypatch.setattr("src.agents.tools.LEGAL_SOURCE_ENABLED", True)
        monkeypatch.setattr("src.agents.tools.PKULAW_ENABLED", False)
        registry = build_default_tools(FakeRetriever())
        names = [t.name for t in registry.list_tools()]
        assert names == ["retrieve_knowledge", "web_search", "legal_source_search"]
        # 三个工具都能产出合法 OpenAI schema
        schemas = registry.to_openai_schemas()
        assert len(schemas) == 3
        for s in schemas:
            assert s["type"] == "function"
            assert s["function"]["parameters"]["type"] == "object"
