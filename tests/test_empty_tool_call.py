"""
空 name tool_call 过滤回归测试（DeepSeek V4 parallel_tool_calls 空占位 Bug，D-M1-6）。

D-M3-13 迁移后：两个后端都走 LangChain，解析逻辑统一收敛到
`src.llm.base.tool_calls_from_langchain()`，不再各自维护一份 `_parse_tool_calls`。
本文件因此改为直接测这个统一入口。

与迁移前的行为差异：LangChain 的 tool_calls 参数是**已解析的 dict**，
不存在「arguments 是非法 JSON 字符串」的解析失败路径（该容错由 LangChain 负责）。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from src.llm.base import tool_calls_from_langchain


def _ai_message(tool_calls: list[dict]) -> AIMessage:
    """构造 LangChain AIMessage（tool_calls 为 LangChain 形态：args 是 dict）。"""
    return AIMessage(
        content="",
        tool_calls=[{"id": tc["id"], "name": tc["name"], "args": tc["args"], "type": "tool_call"} for tc in tool_calls],
    )


class TestToolCallsFromLangChain:
    def test_filters_empty_name(self):
        """空 name 的占位 tool_call 被跳过，正常调用保留。"""
        msg = _ai_message(
            [
                {"id": "c1", "name": "", "args": {}},
                {"id": "c2", "name": "retrieve_knowledge", "args": {"query": "测试"}},
            ]
        )
        calls = tool_calls_from_langchain(msg)
        assert [c.name for c in calls] == ["retrieve_knowledge"]
        assert calls[0].id == "c2"
        assert calls[0].arguments == {"query": "测试"}

    def test_only_empty_name_returns_empty(self):
        """只有空 name 占位 → 结果为空列表（content 即最终答案）。"""
        msg = _ai_message([{"id": "c1", "name": "", "args": {}}])
        assert tool_calls_from_langchain(msg) == []

    def test_normal_calls_unaffected(self):
        """多个正常 tool_call 全部保留（DeepSeek parallel_tool_calls 恒启用）。"""
        msg = _ai_message(
            [
                {"id": "a1", "name": "web_search", "args": {"query": "最新修订"}},
                {"id": "a2", "name": "retrieve_knowledge", "args": {"query": "测试"}},
            ]
        )
        calls = tool_calls_from_langchain(msg)
        assert [c.name for c in calls] == ["web_search", "retrieve_knowledge"]
        assert calls[0].arguments == {"query": "最新修订"}
        assert calls[1].arguments == {"query": "测试"}

    def test_no_tool_calls(self):
        """无 tool_calls 的纯文本回答 → 空列表（content 即最终答案）。"""
        assert tool_calls_from_langchain(AIMessage(content="你好")) == []

    def test_object_without_tool_calls_attr(self):
        """没有 tool_calls 属性的对象不应抛异常（容错）。"""
        assert tool_calls_from_langchain(SimpleNamespace(content="x")) == []
