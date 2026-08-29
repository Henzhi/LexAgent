"""
F12 人工确认节点 —— 技术底座 spike 验证脚本（M3）。

回答 spike 六问中最核心的一问：**LangGraph 的 interrupt() + Command(resume=...)
能否支撑 B 类场景「执行中挂起 → 用户确认 → 恢复执行」的 human-in-the-loop 需求？**

设计原则：
- 状态结构与 src/agents/state.py 的 AgentState 同构（TypedDict + add_messages），
  保证结论可直接迁移到真实代码
- 用 FakeLLM 模拟 ReAct 决策，不依赖真实 LLM / 网络 / API Key
- 同时验证「无 checkpointer 时 interrupt 会失败」，证明 checkpointer 是硬依赖

运行：uv run python scripts/f12_spike_demo.py
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

# ---------------------------------------------------------------------------
# 状态定义（与 AgentState 同构的简化版）
# ---------------------------------------------------------------------------


class SpikeState(TypedDict):
    """与 src/agents/state.py 的 AgentState 同构（仅保留 spike 相关字段）。"""

    query: str
    messages: Annotated[list, add_messages]  # 关键：与线上一致的 reducer
    tool_calls: list
    tool_results: list
    agent_turns: int
    answer: str
    scene: str  # F11 场景分类结果：A（全自动）/ B（需确认）
    confirmed: bool  # 用户是否已确认


# ---------------------------------------------------------------------------
# 节点（模拟 ReAct，不调真实 LLM）
# ---------------------------------------------------------------------------

# 第 0 轮：LLM 决策调用工具；第 1 轮：产出最终答案
_FAKE_ANSWER = "《房屋租赁合同》草稿已生成，含租赁期限、租金支付、违约责任三个核心条款。"


def _fake_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    """OpenAI 格式 tool_call —— 与 react_nodes._tool_calls_to_openai 的输出一致。"""
    import json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def agent_node(state: SpikeState) -> dict:
    """模拟 agent 节点：第 0 轮产出 tool_calls，第 1 轮产出答案。"""
    turns = state.get("agent_turns", 0) + 1
    if turns == 1:
        # 关键：assistant(tool_calls) 消息 —— D-M3-1 中曾因状态覆盖丢失的消息类型
        tc = _fake_tool_call("call_0", "retrieve_knowledge", {"query": "房屋租赁合同 必备条款"})
        return {
            "agent_turns": turns,
            "tool_calls": [tc],
            "messages": [{"role": "assistant", "content": "", "tool_calls": [tc]}],
        }
    return {
        "agent_turns": turns,
        "tool_calls": [],
        "answer": _FAKE_ANSWER,
        "messages": [{"role": "assistant", "content": _FAKE_ANSWER}],
    }


def confirm_node(state: SpikeState) -> dict:
    """B 类场景确认节点：interrupt() 挂起，等待前端确认。

    interrupt() 的返回值即恢复时 Command(resume=...) 传入的值。
    """
    approved = interrupt(
        {
            "type": "confirmation_required",
            "scene": state.get("scene", "B"),
            "prompt": f"即将为「{state.get('query', '')}」生成合同草稿，确认生成范围？",
        }
    )
    return {"confirmed": bool(approved)}


def tools_node(state: SpikeState) -> dict:
    """模拟工具执行节点。"""
    calls = state.get("tool_calls") or []
    results = [{"tool": c["function"]["name"], "ok": True, "summary": f"{c['function']['name']} 执行完成"} for c in calls]
    return {
        "tool_results": results,
        # 工具消息必须与 assistant(tool_calls) 配对（按 id 对应），否则 LLM 400
        "messages": [
            {"role": "tool", "content": r["summary"], "tool_call_id": c["id"]}
            for c, r in zip(calls, results)
        ],
    }


def route_after_agent(state: SpikeState) -> str:
    """agent → confirm（B 类未确认）/ tools（已确认或 A 类）/ END（无 tool_calls）。"""
    if not state.get("tool_calls"):
        return "final"
    if state.get("scene") == "B" and not state.get("confirmed"):
        return "confirm"
    return "tools"


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


def build_spike_graph(checkpointer=None):
    """构建 spike 图。checkpointer=None 时用于验证「无 checkpointer 会失败」。"""
    builder = StateGraph(SpikeState)
    builder.add_node("agent", agent_node)
    builder.add_node("confirm", confirm_node)
    builder.add_node("tools", tools_node)
    builder.set_entry_point("agent")
    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {"confirm": "confirm", "tools": "tools", "final": END},
    )
    builder.add_edge("confirm", "tools")
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


def initial_state(query: str, scene: str) -> dict:
    return {
        "query": query,
        "messages": [{"role": "user", "content": query}],
        "tool_calls": [],
        "tool_results": [],
        "agent_turns": 0,
        "answer": "",
        "scene": scene,
        "confirmed": False,
    }


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------


def _hr(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def verify_hitl() -> bool:
    """验证 1：完整挂起 - 恢复链路。"""
    _hr("验证 1：interrupt 挂起 → Command(resume) 恢复（B 类场景）")

    graph = build_spike_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "user-1:session-1"}}

    # ---- 第一次调用：跑到 interrupt 挂起 ----
    print("▶ 第一次调用（模拟 POST /api/chat/stream）")
    interrupts = []
    for chunk in graph.stream(initial_state("帮我起草房屋租赁合同", "B"), config, stream_mode="updates"):
        for node_name, delta in (chunk or {}).items():
            if node_name == "__interrupt__":
                interrupts.extend(delta)
                print(f"  ⏸  挂起事件：{delta[0].value}")
            else:
                print(f"  ▸ 节点 {node_name} 产出：{list((delta or {}).keys())}")

    ok_interrupt = len(interrupts) == 1
    print(f"  {'✅' if ok_interrupt else '❌'} 挂起事件数量 = {len(interrupts)}（期望 1）")

    # ---- 挂起期间状态是否已持久化 ----
    snapshot = graph.get_state(config)
    persisted = bool(snapshot.values.get("messages"))
    print(f"  {'✅' if persisted else '❌'} 挂起期间状态已持久化：messages={len(snapshot.values.get('messages', []))} 条")
    print(f"  ▸ 下一个待执行节点：{snapshot.next}")

    # ---- 第二次调用：用户确认后恢复 ----
    print("\n▶ 用户点击「确认」（模拟第二个请求带 resume）")
    print("▶ 第二次调用（Command(resume=True) 恢复执行）")
    final = None
    for chunk in graph.stream(Command(resume=True), config, stream_mode="updates"):
        for node_name, delta in (chunk or {}).items():
            if node_name == "__interrupt__":
                print(f"  ⏸  再次挂起：{delta}")
            else:
                print(f"  ▸ 节点 {node_name} 产出：{list((delta or {}).keys())}")
                final = delta

    state = graph.get_state(config).values
    ok_answer = bool(state.get("answer"))
    ok_confirmed = state.get("confirmed") is True
    print(f"  {'✅' if ok_answer else '❌'} 最终答案已生成：{state.get('answer', '(空)')[:40]}...")
    print(f"  {'✅' if ok_confirmed else '❌'} confirmed 字段已置为 True")

    # ---- 关键：messages 完整性（D-M3-1 的历史 Bug 相关） ----
    msgs = state.get("messages", [])
    roles = [m.get("role") if isinstance(m, dict) else getattr(m, "type", "?") for m in msgs]
    has_assistant_toolcall = any(
        (m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None))
        for m in msgs
    )
    print(f"\n  ▸ messages 轨迹：{roles}")
    ok_messages = len(msgs) == 4 and has_assistant_toolcall
    print(
        f"  {'✅' if ok_messages else '❌'} messages 完整（期望 4 条 user/assistant(tool_calls)/tool/assistant，"
        f"实际 {len(msgs)} 条，含 assistant(tool_calls)={has_assistant_toolcall}）"
    )
    print("     —— 挂起/恢复未破坏 add_messages 配对关系")

    return ok_interrupt and ok_answer and ok_confirmed and ok_messages


def verify_no_checkpointer_fails() -> bool:
    """验证 2：无 checkpointer 时的失效模式（证明 checkpointer 是硬依赖）。

    预期不是「报错」，而是更危险的**静默失效**：挂起阶段不抛异常，只是跑不完。
    """
    _hr("验证 2：无 checkpointer 时的失效模式（与线上现状一致）")

    graph = build_spike_graph(checkpointer=None)  # 线上现状：compile() 无 checkpointer
    config = {"configurable": {"thread_id": "user-1:no-cp"}}

    # ① 挂起阶段：不抛异常，只产出 __interrupt__，图就此停住
    saw_interrupt = False
    for chunk in graph.stream(initial_state("帮我起草房屋租赁合同", "B"), config, stream_mode="updates"):
        if "__interrupt__" in (chunk or {}):
            saw_interrupt = True
    print(f"  ▸ ① 挂起阶段：是否产出挂起事件={saw_interrupt}（不抛异常，图静默停住，answer 为空）")

    # ② 状态查询：直接抛错
    err_state = ""
    try:
        graph.get_state(config)
    except Exception as e:
        err_state = f"{type(e).__name__}: {e}"
    print(f"  ▸ ② get_state → {err_state}")

    # ③ 恢复：直接抛错
    err_resume = ""
    try:
        for _ in graph.stream(Command(resume=True), config, stream_mode="updates"):
            pass
    except Exception as e:
        err_resume = f"{type(e).__name__}: {e}"
    print(f"  ▸ ③ Command(resume=True) → {err_resume}")

    ok = saw_interrupt and "No checkpointer" in err_state and "without checkpointer" in err_resume
    if ok:
        print("  ✅ 确认 checkpointer 是硬依赖")
        print("     ⚠️  失效模式极隐蔽：挂起阶段不报错也不出答案，前端只收到挂起事件后")
        print("        永远等不到结果，后端日志无异常 —— 必须在代码中显式自检")
    else:
        print("  ❌ 行为与预期不符，需复查")
    return ok


def verify_a_class_unaffected() -> bool:
    """验证 3：A 类场景不受影响（不挂起，一次跑完）。"""
    _hr("验证 3：A 类场景不触发确认（回归保障）")

    graph = build_spike_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "user-1:session-2"}}
    interrupts = []
    for chunk in graph.stream(initial_state("劳动合同法第四十六条是什么", "A"), config, stream_mode="updates"):
        for node_name, _ in (chunk or {}).items():
            if node_name == "__interrupt__":
                interrupts.append(node_name)

    state = graph.get_state(config).values
    ok = not interrupts and bool(state.get("answer"))
    print(f"  {'✅' if ok else '❌'} A 类场景零挂起、正常出答案（挂起数={len(interrupts)}）")
    return ok


def verify_thread_isolation() -> bool:
    """验证 4：thread_id 隔离 —— 不同会话互不干扰。"""
    _hr("验证 4：thread_id 隔离（多用户并发安全）")

    graph = build_spike_graph(checkpointer=MemorySaver())
    cfg_a = {"configurable": {"thread_id": "user-1:session-A"}}
    cfg_b = {"configurable": {"thread_id": "user-2:session-B"}}

    for cfg, q in ((cfg_a, "起草租赁合同"), (cfg_b, "起草劳动合同")):
        for _ in graph.stream(initial_state(q, "B"), cfg, stream_mode="updates"):
            pass

    sa = graph.get_state(cfg_a).values
    sb = graph.get_state(cfg_b).values
    ok = sa.get("query") == "起草租赁合同" and sb.get("query") == "起草劳动合同"
    print(f"  {'✅' if ok else '❌'} 两个 thread 状态互不覆盖：A={sa.get('query')} / B={sb.get('query')}")
    print("     —— thread_id 可用 `user_id:session_id` 构造（ChatRequest 已有 session_id 字段）")
    return ok


def verify_resume_reject() -> bool:
    """验证 5：用户拒绝（resume=False）时的行为。"""
    _hr("验证 5：用户拒绝（resume=False）")

    graph = build_spike_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "user-1:session-3"}}
    for _ in graph.stream(initial_state("帮我起草房屋租赁合同", "B"), config, stream_mode="updates"):
        pass
    for _ in graph.stream(Command(resume=False), config, stream_mode="updates"):
        pass

    state = graph.get_state(config).values
    ok = state.get("confirmed") is False
    print(f"  {'✅' if ok else '❌'} 拒绝后 confirmed={state.get('confirmed')}（图继续跑完，未卡死）")
    print("     —— 注意：拒绝语义需业务层处理（跳过生成或改用默认参数），框架只负责传值")
    return ok


def main() -> int:
    print("F12 人工确认节点 —— 技术底座 spike")
    print(f"{'=' * 68}")
    results = {
        "1 挂起-恢复链路": verify_hitl(),
        "2 无 checkpointer 失败": verify_no_checkpointer_fails(),
        "3 A 类不受影响": verify_a_class_unaffected(),
        "4 thread_id 隔离": verify_thread_isolation(),
        "5 拒绝分支": verify_resume_reject(),
    }
    _hr("结论")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} 验证 {name}")
    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\n❌ 未通过：{', '.join(failed)}")
        return 1
    print("\n✅ 全部通过：LangGraph interrupt + checkpointer 可支撑 F12 human-in-the-loop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
