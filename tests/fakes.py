"""
M1 测试共用替身（fake retriever / fake tool LLM）。

不依赖外部服务；供 test_tools / test_react_agent / test_failover 等模块复用。
"""
from __future__ import annotations


class FakeRetriever:
    """返回固定检索结果的假检索器（支持 doc_type 参数）。"""

    def __init__(self, docs=None):
        from src.rag.retriever import RetrievedDoc
        self.docs = docs or [
            RetrievedDoc(
                content="根据《测试法》第一条，测试规定内容。",
                score=0.9,
                law_name="《测试法》",
                chapter="第一章",
                section="",
                article_range="第一条",
            )
        ]

    def search(self, query: str, top_k: int = 5, doc_type: str | None = None):
        return self.docs[:top_k]

    def is_ready(self) -> bool:
        return True


class FakeToolLLM:
    """可脚本化工具调用的假 LLM（实现 LLMAdapter 兼容接口）。

    script: list[ToolCallResponse] 或 callable(调用次数)->ToolCallResponse。
    每次 chat_with_tools 消费一个脚本条目；耗尽后返回"已耗尽"答案。
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.model = "fake-model"
        self.temperature = 0.1

    def chat(self, user_message, history=None, system_prompt=None):
        return "PASS\n理由：未发现幻觉"

    def chat_stream(self, user_message, history=None, system_prompt=None):
        yield "fake-token"

    def chat_with_tools(self, messages, tools, tool_choice="auto"):
        from src.llm.base import ToolCallResponse
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        if not self.script:
            return ToolCallResponse(content="已耗尽脚本，直接回答。", tool_calls=[])
        item = self.script.pop(0)
        if callable(item):
            return item(len(self.calls))
        return item

    def get_context_window(self):
        return 32000
