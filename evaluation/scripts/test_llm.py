"""
LLM 客户端测试脚本。

用法:
    uv run python scripts/test_llm.py              # 普通对话
    uv run python scripts/test_llm.py --stream     # 流式对话
    uv run python scripts/test_llm.py --rag        # 带法律条文的问答
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.llm.client import LawLLM, Message, LLMConfig


def test_basic_chat():
    """基本对话测试"""
    print("=" * 60)
    print("  测试 1: 基本对话")
    print("=" * 60)

    llm = LawLLM()
    reply = llm.chat("法律上的正当防卫和紧急避险有什么区别？")
    print(f"\n回答:\n{reply}\n")


def test_multi_turn():
    """多轮对话测试"""
    print("=" * 60)
    print("  测试 2: 多轮对话（带历史）")
    print("=" * 60)

    llm = LawLLM()

    history = []
    q1 = "治安管理处罚的种类有哪些？"
    a1 = llm.chat(q1, history)
    print(f"\nQ: {q1}")
    print(f"A: {a1}")

    history.append(Message.user(q1))
    history.append(Message.assistant(a1))

    q2 = "行政拘留最长多久？"
    a2 = llm.chat(q2, history)
    print(f"\nQ: {q2}")
    print(f"A: {a2}")


def test_stream():
    """流式输出测试"""
    print("=" * 60)
    print("  测试 3: 流式输出")
    print("=" * 60)

    llm = LawLLM()
    print("\n回答: ", end="", flush=True)
    for token in llm.chat_stream("简述一下民法典的七编分别是什么"):
        print(token, end="", flush=True)
    print("\n")


def test_rag():
    """带上下文的法律问答测试"""
    print("=" * 60)
    print("  测试 4: RAG 问答（带法律条文上下文）")
    print("=" * 60)

    context = """【中华人民共和国治安管理处罚法(2025修订)】／第二章　处罚的种类和适用
第十条　治安管理处罚的种类分为：
（一）警告；
（二）罚款；
（三）行政拘留；
（四）吊销公安机关发放的许可证件。
对违反治安管理的外国人，可以附加适用限期出境或者驱逐出境。
第十六条　有两种以上违反治安管理行为的，分别决定，合并执行处罚。行政拘留处罚合并执行的，最长不超过二十日。"""

    llm = LawLLM()
    reply = llm.chat_with_context(
        "行政拘留合并执行最长几天？",
        context,
    )
    print(f"\n回答:\n{reply}\n")


def test_langchain_interface():
    """LangChain 接口兼容性测试"""
    print("=" * 60)
    print("  测试 5: LangChain BaseChatModel 接口")
    print("=" * 60)

    from langchain_core.messages import HumanMessage, SystemMessage

    llm = LawLLM()
    messages = [
        SystemMessage(content="你是法律助手，回答简洁"),
        HumanMessage(content="民事行为能力分哪几种？"),
    ]
    result = llm.invoke(messages)
    print(f"\n回答:\n{result.content}\n")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="LLM 客户端测试")
    ap.add_argument("--basic", action="store_true", help="基本对话")
    ap.add_argument("--multi", action="store_true", help="多轮对话")
    ap.add_argument("--stream", action="store_true", help="流式输出")
    ap.add_argument("--rag", action="store_true", help="RAG 问答")
    ap.add_argument("--langchain", action="store_true", help="LangChain 接口")

    args = ap.parse_args()

    # 默认运行全部
    run_all = not any([args.basic, args.multi, args.stream, args.rag, args.langchain])

    try:
        if run_all or args.basic:
            test_basic_chat()
        if run_all or args.multi:
            test_multi_turn()
        if run_all or args.stream:
            test_stream()
        if run_all or args.rag:
            test_rag()
        if run_all or args.langchain:
            test_langchain_interface()

        print("=" * 60)
        print("  全部测试通过！")
        print("=" * 60)
    except Exception as e:
        print(f"\n测试失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
