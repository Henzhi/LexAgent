"""意图识别单元测试 — classify_intent()"""
from __future__ import annotations

import pytest
from src.rag.intent import classify_intent


@pytest.mark.parametrize("query,expected", [
    # 闲聊
    ("你好", False),
    ("你好！", False),
    ("您好", False),
    ("在吗", False),
    ("在不在", False),
    ("谢谢", False),
    ("谢谢你", False),
    ("非常感谢", False),
    ("再见", False),
    ("拜拜", False),
    ("你是谁", False),
    ("我是谁", False),
    ("我是痕至", False),
    ("我叫小明", False),
    ("我姓王", False),
    ("我是干什么的", False),
    ("你认识我吗", False),
    ("还记得我吗", False),
    ("你能做什么", False),
    ("测试", False),
    ("hi", False),
    ("hello", False),
    ("thanks", False),
    # 法律
    ("治安处罚有哪些种类", True),
    ("他打了我怎么办", True),
    ("什么是正当防卫", True),
    ("合同违约怎么赔偿", True),
    ("酒驾怎么处罚", True),
    ("故意伤害罪判多久", True),
    ("民法典关于婚姻的规定", True),
    ("劳动法辞退赔偿", True),
    ("工伤认定标准", True),
    ("诉讼时效多久", True),
    ("起诉需要什么材料", True),
    ("合法吗", True),
    ("违法吗", True),
    ("判多久", True),
    # 边界
    ("帮我查一下治安法", True),  # 含关键词
    ("", True),                  # 空字符默认检索
    ("？？？", True),            # 纯标点 → 标准化后空 → 默认检索
])
def test_classify_intent(query, expected):
    assert classify_intent(query) == expected


def test_normalize():
    """测试标准化函数"""
    from src.rag.intent import _normalize
    assert _normalize("你好！") == "你好"
    assert _normalize("HI") == "hi"
    assert _normalize("HELLO, World!") == "helloworld"
    assert _normalize("治安管理处罚法  ") == "治安管理处罚法"
    assert _normalize("？？？") == ""
