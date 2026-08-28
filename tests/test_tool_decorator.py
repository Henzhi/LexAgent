"""
@tool 装饰器测试（M3）：从类型注解 + Annotated 元数据推导 OpenAI JSON Schema。

装饰器只做语法糖，产出与手写 class 一致的 ToolSpec，不引入任何第三方依赖。
"""
from __future__ import annotations

from typing import Annotated

from src.agents.tools.base import (
    CATEGORY_LEGAL,
    Param,
    ToolResult,
    ToolSpec,
    tool,
)


class TestToolDecoratorBasics:
    def test_returns_toolspec_with_executor(self):
        """装饰器产出 ToolSpec，executor 即原函数。"""

        @tool(name="my_tool", category=CATEGORY_LEGAL)
        def my_tool(query: Annotated[str, "查询"] ) -> ToolResult:
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
        assert props["top_k"] == {"type": "integer", "description": "条数"}
        assert props["ratio"] == {"type": "number"}
        assert props["flag"] == {"type": "boolean"}
        assert t.required == ["query"]

    def test_optional_type_unwraps_to_base_type(self):
        """Optional[X] / X | None → X 的类型，可选性只体现在 required。"""

        @tool(name="t")
        def t(query: str, doc_type: str | None = None) -> ToolResult:
            """Optional 测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["doc_type"]["type"] == "string"
        assert "null" not in str(props["doc_type"])
        assert t.required == ["query"]

    def test_param_enum_and_description(self):
        """Param 元数据：description + enum。"""

        @tool(name="t")
        def t(
            query: str,
            source_type: Annotated[str, Param("来源类型", enum=["law", "case", "all"])] = "law",
        ) -> ToolResult:
            """enum 测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["source_type"]["enum"] == ["law", "case", "all"]
        assert props["source_type"]["description"] == "来源类型"

    def test_unknown_type_falls_back_to_string(self):
        """无法识别的类型降级为 string（不让 schema 卡死 LLM）。"""
        from src.rag.retriever import RetrievedDoc

        @tool(name="t")
        def t(doc: RetrievedDoc) -> ToolResult:  # type: ignore[valid-type]
            """未知类型测试。"""
            return ToolResult(tool="t", call_id="", ok=True, summary="")

        props = t.to_openai_format()["function"]["parameters"]["properties"]
        assert props["doc"]["type"] == "string"

    def test_openai_format_shape(self):
        """产出结构对齐 OpenAI tools 参数格式。"""

        @tool(name="t", category=CATEGORY_LEGAL)
        def t(query: Annotated[str, "q"] ) -> ToolResult:
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
        assert props["doc_type"]["enum"] == ["law", "case"]
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
        """build_default_tools 注册数量与名称不变（集成回归）。"""
        from src.agents.tools import build_default_tools
        from tests.fakes import FakeRetriever

        monkeypatch.setattr("src.agents.tools.LEGAL_SOURCE_ENABLED", True)
        registry = build_default_tools(FakeRetriever())
        names = [t.name for t in registry.list_tools()]
        assert names == ["retrieve_knowledge", "web_search", "legal_source_search"]
        # 三个工具都能产出合法 OpenAI schema
        schemas = registry.to_openai_schemas()
        assert len(schemas) == 3
        for s in schemas:
            assert s["type"] == "function"
            assert s["function"]["parameters"]["type"] == "object"
