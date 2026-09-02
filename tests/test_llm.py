"""Message 数据类单元测试（2026-09-01 审查整改 B8：自 src/llm/client.py 迁入 base.py）。

原 test_llm.py 同时覆盖死代码 client.py 的 LawLLM / LLMConfig——随 client.py
删除，仅保留仍有生产用途的 Message 用例。
"""

from src.llm.base import Message


# ============================================================
# Message — 消息模型
# ============================================================


def test_message_basic():
    msg = Message(role="user", content="你好")
    assert msg.role == "user"
    assert msg.content == "你好"


def test_message_to_dict():
    msg = Message(role="system", content="系统提示")
    d = msg.to_dict()
    assert d == {"role": "system", "content": "系统提示"}


def test_message_system_factory():
    msg = Message.system("你是法律助手")
    assert msg.role == "system"
    assert msg.content == "你是法律助手"


def test_message_user_factory():
    msg = Message.user("刑法第二十条")
    assert msg.role == "user"
    assert "刑法第二十条" == msg.content


def test_message_assistant_factory():
    msg = Message.assistant("根据刑法...")
    assert msg.role == "assistant"
    assert "刑法" in msg.content
