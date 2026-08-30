"""
M1 测试共用替身（fake retriever / fake tool LLM）。

不依赖外部服务；供 test_tools / test_react_agent / test_failover 等模块复用。

D-M3-13：`FakeToolLLM` 额外提供 `chat_model` 属性，把脚本化响应包装成
LangChain ChatModel 形态（bind_tools / invoke），使既有测试无需改动即可
覆盖迁移后的 LangChain 代码路径。
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

    @property
    def chat_model(self):
        """LangChain 标准接口（D-M3-13）——包装成本版 ChatModel 形态。

        让迁移后的 `agent_node`（走 bind_tools + invoke）仍能消费脚本化响应。
        """
        return _FakeChatModel(self)


class _FakeChatModel:
    """把 FakeToolLLM 的脚本化响应包装成 LangChain ChatModel 形态。

    只实现 agent_node 用到的两个方法：`bind_tools()` 与 `invoke()`。
    invoke 时把 LangChain 消息转回项目内部的 OpenAI dict 格式再交给
    FakeToolLLM，保证既有测试对 `calls[0]["messages"]` 的断言不受影响。
    """

    def __init__(self, owner, tools=None, **bind_kwargs):
        self._owner = owner
        self._tools = tools or []
        self._bind_kwargs = bind_kwargs

    def bind_tools(self, tools, **kwargs):
        return _FakeChatModel(self._owner, tools, **kwargs)

    def invoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage, convert_to_openai_messages

        # LangChain Message 对象 → 项目内部的 OpenAI dict 格式
        dict_messages = convert_to_openai_messages(messages)
        resp = self._owner.chat_with_tools(dict_messages, self._tools)
        return AIMessage(
            content=resp.content or "",
            tool_calls=[
                {
                    "id": tc.id,
                    "name": tc.name,
                    "args": tc.arguments,
                    "type": "tool_call",
                }
                for tc in (resp.tool_calls or [])
            ],
        )


class FakePkulawClient:
    """北大法宝 MCP 客户端替身（不触达真实端点 / Key，供 pkulaw 相关单测）。

    实现 PkulawMCPClient 的同名公开方法，返回 canned 数据；is_available 可配置。
    返回条目含 `#tiao_1077.0` 锚点以覆盖 _strip_dotzero 归一化路径。
    """

    def __init__(self, available: bool = True):
        self._available = available
        self.article = [
            {
                "title": "中华人民共和国民法典",
                "url": "https://pkulaw.com/lawext?tid=1#tiao_1077.0",
                "content": "第一条 为了保护民事主体的合法权益……",
                "law_status": "现行有效",
                "effectiveness": "法律",
            }
        ]
        self.case = [
            {
                "title": "张三诉李四买卖合同纠纷案",
                "url": "https://pkulaw.com/case/1",
                "content": "本院查明……本院认为……裁判结果……",
                "case_number": "(2024)京01民终123号",
                "court": "北京市第一中级人民法院",
            }
        ]
        self.law_list = [
            {
                "title": "数据安全法",
                "url": "https://pkulaw.com/l/2",
                "content": "",
                "law_status": "现行有效",
            }
        ]
        self.verify = [{"title": "民法典", "original": "中华人民共和国民法典"}]
        self.linked = "[《民法典》第1077条](https://pkulaw.com/lawext?tid=1#tiao_1077)"

    def is_available(self) -> bool:
        return self._available

    def search_article(self, query, lib="中央", max_results=5):
        return list(self.article)

    def get_article(self, title, number):
        return list(self.article)

    def search_case(self, query, max_results=5):
        return list(self.case)

    def get_law_list(self, title, effectiveness=None, max_results=5):
        return list(self.law_list)

    def verify_law(self, text):
        return list(self.verify)

    def verify_case(self, text):
        return list(self.verify)

    def verify_provision(self, userlaw, answerlaw, prompt=""):
        return {"compared": True, "match": True}

    def add_links(self, text):
        return self.linked
