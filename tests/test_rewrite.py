"""查询改写模块测试：正常规范化、失败回退、去引号。"""
from src.agents.rewrite import rewrite_query


class _FakeLLM:
    def __init__(self, out):
        self.out = out

    def chat(self, prompt, history=None, system_prompt=None):
        return self.out


class _BoomLLM:
    def chat(self, prompt, history=None, system_prompt=None):
        raise RuntimeError("llm down")


def test_rewrite_normalizes_colloquial():
    llm = _FakeLLM("用人单位拖欠劳动报酬的法律责任与维权途径")
    assert rewrite_query(llm, "老板拖欠工资") == "用人单位拖欠劳动报酬的法律责任与维权途径"


def test_rewrite_fallback_on_error():
    assert rewrite_query(_BoomLLM(), "老板拖欠工资") == "老板拖欠工资"


def test_rewrite_strips_quotes():
    llm = _FakeLLM('"刑法第232条"')
    assert rewrite_query(llm, "刑法第232条是什么") == "刑法第232条"
