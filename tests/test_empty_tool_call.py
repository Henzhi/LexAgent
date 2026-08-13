"""
空 name tool_call 解析回归测试（DeepSeek V4 parallel_tool_calls 空占位 Bug）。

覆盖：
- OpenAI 兼容后端 _parse_tool_calls：空 name 的占位 tool_call 被过滤，正常调用不受影响
- Ollama 后端 _parse_tool_calls：同上（三处解析逻辑保持一致）
"""
from __future__ import annotations

from types import SimpleNamespace

from src.llm.ollama_backend import OllamaBackend
from src.llm.openai_backend import OpenAICompatibleBackend


def _openai_message(tool_calls: list[dict]) -> SimpleNamespace:
    """构造形如 openai SDK response message 的假对象。"""
    return SimpleNamespace(tool_calls=[
        SimpleNamespace(
            id=tc["id"],
            function=SimpleNamespace(name=tc["name"], arguments=tc["arguments"]),
        )
        for tc in tool_calls
    ])


class TestOpenAIParseToolCalls:
    def test_filters_empty_name(self):
        """空 name 的占位 tool_call 被跳过，正常调用保留。"""
        msg = _openai_message([
            {"id": "c1", "name": "", "arguments": "{}"},
            {"id": "c2", "name": "retrieve_knowledge", "arguments": '{"query":"测试"}'},
        ])
        calls = OpenAICompatibleBackend._parse_tool_calls(msg)
        assert [c.name for c in calls] == ["retrieve_knowledge"]
        assert calls[0].id == "c2"
        assert calls[0].arguments == {"query": "测试"}
        assert calls[0].parse_error == ""

    def test_only_empty_name_returns_empty(self):
        """只有空 name 占位 → 解析结果为空列表（content 即最终答案）。"""
        msg = _openai_message([
            {"id": "c1", "name": "", "arguments": ""},
        ])
        assert OpenAICompatibleBackend._parse_tool_calls(msg) == []

    def test_normal_calls_unaffected(self):
        """正常 tool_call 解析语义不变（含非法 JSON 的 parse_error）。"""
        msg = _openai_message([
            {"id": "a1", "name": "web_search", "arguments": '{"query":"最新修订"}'},
            {"id": "a2", "name": "retrieve_knowledge", "arguments": "{bad"},
        ])
        calls = OpenAICompatibleBackend._parse_tool_calls(msg)
        assert [c.name for c in calls] == ["web_search", "retrieve_knowledge"]
        assert calls[0].arguments == {"query": "最新修订"}
        assert calls[0].parse_error == ""
        assert calls[1].arguments == {}
        assert calls[1].parse_error != ""


class TestOllamaParseToolCalls:
    def test_filters_empty_name(self):
        """Ollama dict 形态：空 name 占位被跳过。"""
        msg = {
            "tool_calls": [
                {"id": "c1", "function": {"name": "", "arguments": {}}},
                {"id": "c2", "function": {"name": "retrieve_knowledge", "arguments": {"query": "测试"}}},
            ]
        }
        calls = OllamaBackend._parse_tool_calls(msg)
        assert [c.name for c in calls] == ["retrieve_knowledge"]
        assert calls[0].arguments == {"query": "测试"}

    def test_only_empty_name_returns_empty(self):
        msg = {"tool_calls": [{"id": "c1", "function": {"name": "", "arguments": {}}}]}
        assert OllamaBackend._parse_tool_calls(msg) == []

    def test_normal_calls_unaffected(self):
        msg = {
            "tool_calls": [
                {"id": "a1", "function": {"name": "web_search", "arguments": {"query": "x"}}},
                {"id": "a2", "function": {"name": "retrieve_knowledge", "arguments": '{"query":"测试"}'}},
            ]
        }
        calls = OllamaBackend._parse_tool_calls(msg)
        assert [c.name for c in calls] == ["web_search", "retrieve_knowledge"]
        assert calls[0].arguments == {"query": "x"}
        # 字符串形式的 arguments 同样被解析
        assert calls[1].arguments == {"query": "测试"}
