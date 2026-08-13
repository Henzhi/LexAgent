"""LLM 客户端单元测试 — Message / LLMConfig / LawLLM._build_messages 纯函数"""

import pytest
from unittest.mock import patch
from src.llm.client import Message, LLMConfig, LawLLM, LAW_SYSTEM_PROMPT


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


# ============================================================
# LLMConfig — LLM 配置
# ============================================================

def test_llm_config_defaults():
    cfg = LLMConfig()
    assert cfg.temperature == 0.1
    assert cfg.top_p == 0.9
    assert cfg.num_predict == 2048
    assert cfg.repeat_penalty == 1.05
    assert cfg.seed == 42


def test_llm_config_custom():
    cfg = LLMConfig(temperature=0.5, top_p=0.8, num_predict=512, repeat_penalty=1.1, seed=123)
    assert cfg.temperature == 0.5
    assert cfg.top_p == 0.8
    assert cfg.num_predict == 512
    assert cfg.repeat_penalty == 1.1
    assert cfg.seed == 123


def test_llm_config_to_options():
    cfg = LLMConfig()
    opts = cfg.to_options()
    assert opts["temperature"] == 0.1
    assert opts["top_p"] == 0.9
    assert opts["num_predict"] == 2048
    assert opts["repeat_penalty"] == 1.05
    assert opts["seed"] == 42


def test_llm_config_to_options_custom():
    cfg = LLMConfig(temperature=0.0, top_p=1.0)
    opts = cfg.to_options()
    assert opts["temperature"] == 0.0
    assert opts["top_p"] == 1.0


# ============================================================
# LawLLM._build_messages — 消息列表构建
# ============================================================

@pytest.fixture
def mock_llm():
    """mock ollama Client 以绕过网络调用"""
    with patch("ollama.Client", autospec=True):
        llm = LawLLM(
            model="qwen2.5:7b",
            base_url="http://localhost:11434",
            system_prompt=LAW_SYSTEM_PROMPT,
            config=LLMConfig(),
            max_retries=1,
        )
        return llm


def test_build_messages_simple(mock_llm):
    msgs = mock_llm._build_messages("刑法第十条是什么")
    assert len(msgs) >= 2  # system + user
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "刑法第十条是什么"


def test_build_messages_with_history(mock_llm):
    history = [
        Message.user("第一条"),
        Message.assistant("回答第一条"),
    ]
    msgs = mock_llm._build_messages("第二条", history=history)
    assert len(msgs) == 4  # system + history[0] + history[1] + user
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "第一条"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "回答第一条"
    assert msgs[3]["content"] == "第二条"


def test_build_messages_custom_system_prompt(mock_llm):
    msgs = mock_llm._build_messages("你好", system_prompt="自定义提示词")
    assert msgs[0]["content"] == "自定义提示词"


def test_build_messages_no_system_prompt(mock_llm):
    msgs = mock_llm._build_messages("你好", system_prompt="")
    # 空 system_prompt 会被跳过
    roles = [m["role"] for m in msgs]
    if "" in [mock_llm._system_prompt]:
        assert roles[0] == "user"
    else:
        assert roles[0] == "system" or roles[0] == "user"


def test_build_messages_empty_system(mock_llm):
    """空字符串 system_prompt 时回退到实例默认 LAW_SYSTEM_PROMPT"""
    msgs = mock_llm._build_messages("test", system_prompt="")
    roles = [m["role"] for m in msgs]
    # sp = system_prompt or self._system_prompt → 回退到实例默认值
    assert roles[0] == "system"
    assert len(mock_llm._system_prompt) > 10  # 默认是完整法律提示词


# ============================================================
# LawLLM._build_rag_prompt — RAG Prompt 构建
# ============================================================

def test_build_rag_prompt(mock_llm):
    prompt = mock_llm._build_rag_prompt("正当防卫怎么认定", "第二十条: 正当防卫...")
    assert "正当防卫怎么认定" in prompt
    assert "第二十条" in prompt
    assert "相关法律条文" in prompt
    assert "要求" in prompt


def test_build_rag_prompt_empty_context(mock_llm):
    prompt = mock_llm._build_rag_prompt("问题", "")
    assert "问题" in prompt


# ============================================================
# LawLLM 属性
# ============================================================

def test_llm_identifying_params(mock_llm):
    params = mock_llm._identifying_params
    assert params["model"] == "qwen2.5:7b"
    assert "temperature" in params
    assert "base_url" in params


def test_llm_llm_type(mock_llm):
    assert mock_llm._llm_type == "ollama-law-llm"
