"""
ReAct 循环节点（M1 / F2，架构师 D1：手动 StateGraph + 自研 chat_with_tools）。

提供：
- agent_node：调用 llm.chat_with_tools(messages, tools_schema) 决策；返回 tool_calls
  （写入 state.tool_calls）或最终答案（写入 state.answer）；agent_turns += 1。
  达到轮数上限时移除 tools（强制产出答案，REQ-UW4）。
- tools_node：遍历 state.tool_calls 经 ToolRegistry.execute 执行（并行 tool_calls 全部执行，
  DeepSeek V4 parallel_tool_calls 恒启用），结果写 state.tool_results / tool_log /
  retrieved_docs；构造 tool 角色消息回灌 state.messages；消费后清空 tool_calls。
- route_after_agent / route_after_tools：条件路由函数。

工具回灌消息格式（共享约定 §8.4）：
  {"role":"assistant","tool_calls":[...]} + {"role":"tool","tool_call_id":"...","content":"..."}
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from src.agents.prompts import REACT_SYSTEM_PROMPT
from src.agents.state import AgentState
from src.agents.tools.base import SOURCE_INTERNAL_KB, SOURCE_LEGAL, SOURCE_WEB, ToolResult
from src.agents.tools.registry import ToolRegistry
from src.llm.base import (
    ToolCall,
    ToolCallResponse,
    to_langchain_messages,
    tool_calls_from_langchain,
)

logger = logging.getLogger(__name__)


# DeepSeek 在无 tools 参数时仍可能以纯文本输出 DSML 工具调用语法
# （达到轮数上限被强制作答时），兜底清除保证最终答案是自然语言。
_DSML_TOOL_CALLS_RE = re.compile(
    r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>", re.DOTALL
)


def _strip_dsml_tool_calls(text: str) -> str:
    """清除文本中的 DSML 工具调用块（轮数上限强制作答的兜底清洗）。"""
    if not text or "DSML" not in text:
        return text
    return _DSML_TOOL_CALLS_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# 消息转换工具
# ---------------------------------------------------------------------------

def _tool_calls_to_openai(tool_calls: list[Any]) -> list[dict]:
    """LangChain / ToolCall 对象 → OpenAI 格式 tool_calls（供 assistant 消息回灌）。"""
    result: list[dict] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            if "function" in tc:
                # 已是 OpenAI 原始格式
                result.append(tc)
            else:
                # LangChain dict 格式 {"name","args","id","type"}
                result.append({
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False),
                    },
                })
        else:
            # ToolCall dataclass / langchain ToolCall 对象
            result.append({
                "id": getattr(tc, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(tc, "name", ""),
                    "arguments": json.dumps(getattr(tc, "arguments", getattr(tc, "args", {})), ensure_ascii=False),
                },
            })
    return result


def _messages_to_dicts(messages: list[Any]) -> list[dict]:
    """state.messages → dict 列表（兼容 dict 与 LangChain BaseMessage 对象）。

    LangGraph 的 add_messages reducer 会把 dict 转为 BaseMessage；LLM 调用需要 dict。
    """
    result: list[dict] = []
    for m in messages or []:
        if isinstance(m, dict):
            result.append(dict(m))
        elif hasattr(m, "type"):
            role_map = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
            role = role_map.get(getattr(m, "type", ""), "user")
            content = getattr(m, "content", "") or ""
            d: dict[str, Any] = {"role": role, "content": str(content)}
            if role == "assistant" and getattr(m, "tool_calls", None):
                d["tool_calls"] = _tool_calls_to_openai(getattr(m, "tool_calls"))
            if role == "tool":
                d["tool_call_id"] = getattr(m, "tool_call_id", "")
            result.append(d)
    return result


def _build_react_messages(state: AgentState, system_prompt: str) -> list[dict]:
    """组装 ReAct 决策消息：system(REACT_SYSTEM_PROMPT) + memory + history/工具回灌 + 当前问题。"""
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    memory = state.get("memory_context", "") or ""
    if memory:
        messages.append({"role": "system", "content": f"## 历史对话记忆\n{memory}"})
    messages.extend(_messages_to_dicts(state.get("messages", [])))
    query = state.get("query", "") or ""
    if query:
        last = messages[-1] if messages else None
        if not (last and last.get("role") == "user" and last.get("content") == query):
            messages.append({"role": "user", "content": query})
    return messages


def _merge_docs(existing: list[dict], new_docs: list[dict]) -> list[dict]:
    """合并检索文档并去重（按 法名+条号+内容前缀）。"""
    seen = {
        (d.get("law_name", ""), d.get("article_range", ""), (d.get("content") or "")[:60])
        for d in existing
    }
    result = list(existing)
    for d in new_docs:
        key = (d.get("law_name", ""), d.get("article_range", ""), (d.get("content") or "")[:60])
        if key in seen:
            continue
        seen.add(key)
        result.append(d)
    return result


def _merge_by_url(existing: list[dict], new_items: list[dict]) -> list[dict]:
    """合并网络/官方源证据并按 URL 去重（M2 / F7）。"""
    seen = {(r.get("url") or r.get("title") or "") for r in existing}
    result = list(existing)
    for r in new_items:
        key = (r.get("url") or r.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(r)
    return result


# ---------------------------------------------------------------------------
# ReAct 节点工厂
# ---------------------------------------------------------------------------

def make_react_nodes(
    llm,
    registry: ToolRegistry,
    system_prompt: str | None = None,
    max_tool_turns: int = 5,
) -> dict[str, Callable]:
    """创建 ReAct 节点（闭包注入外部依赖，节点本身无状态）。

    Args:
        llm: LLM 实例（LLMAdapter 或实现 chat_with_tools 的对象）
        registry: 工具注册表（提供 to_openai_schemas / execute）
        system_prompt: 决策系统提示词，默认 REACT_SYSTEM_PROMPT
        max_tool_turns: 工具调用轮数上限（REQ-UW4）

    Returns:
        {"agent": agent_node, "tools": tools_node,
         "route_after_agent": ..., "route_after_tools": ...}
    """
    sp = system_prompt or REACT_SYSTEM_PROMPT
    max_turns = max(1, int(max_tool_turns))

    def agent_node(state: AgentState) -> dict:
        """LLM 决策节点：返回 tool_calls 或最终答案。"""
        turns = state.get("agent_turns", 0) or 0
        messages = _build_react_messages(state, sp)
        # 达到轮数上限 → 移除工具，强制模型产出最终答案（REQ-UW4）
        schemas = registry.to_openai_schemas() if turns < max_turns else []
        if not schemas:
            # 明确告知模型不能再调工具，否则 DeepSeek 会在纯文本里
            # 输出 DSML 工具调用语法而非自然语言答案
            messages.append({
                "role": "system",
                "content": (
                    "你已达到工具调用轮数上限。请立即基于已获取的工具结果直接回答用户问题，"
                    "禁止再输出任何工具调用语法。"
                ),
            })
        update: dict[str, Any] = {
            "agent_turns": turns + 1,
            "tool_results": [],
        }
        try:
            # D-M3-13：LangChain 标准写法 —— bind_tools + invoke。
            # 预算埋点由 ChatModel 上挂载的 LLMBudgetCallbackHandler 负责（F14）。
            chat_model = llm.chat_model
            bound = (
                chat_model.bind_tools(schemas, tool_choice="auto")
                if schemas
                else chat_model
            )
            ai_message = bound.invoke(to_langchain_messages(messages))
            resp = ToolCallResponse(
                content=ai_message.content or "",
                tool_calls=tool_calls_from_langchain(ai_message),
            )
        except Exception as e:
            logger.error(f"agent 节点 LLM 工具调用失败: {e}", exc_info=True)
            update["tool_calls"] = []
            update["answer"] = "抱歉，模型调用失败，暂时无法回答该问题。"
            update["messages"] = [{"role": "assistant", "content": update["answer"]}]
            return update

        # 防御性兜底：过滤 name 为空的 ToolCall（DeepSeek V4 想直接回答时
        # 可能返回函数名为空的占位 tool_call，不应路由到 tools）。
        calls: list[ToolCall] = [
            tc for tc in (resp.tool_calls or []) if getattr(tc, "name", "")
        ]
        if calls and schemas:
            update["tool_calls"] = calls
            update["messages"] = [{
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": _tool_calls_to_openai(calls),
            }]
        else:
            # 无工具可用 / 模型未返回工具调用 → content 即最终答案
            answer = _strip_dsml_tool_calls((resp.content or "").strip()) or "抱歉，暂时无法回答该问题。"
            update["tool_calls"] = []
            update["answer"] = answer
            update["messages"] = [{"role": "assistant", "content": answer}]
        return update

    def tools_node(state: AgentState) -> dict:
        """执行本轮全部 tool_calls（串行，数量有限），结果回灌 messages。

        三路证据累计（M2 / F6）：internal_kb → retrieved_docs；
        web → web_results；legal_source → legal_results（供最终融合 F7/F8）。
        """
        calls: list[ToolCall] = list(state.get("tool_calls", []) or [])
        tool_results: list[ToolResult] = []
        tool_messages: list[dict] = []
        log_entries: list[dict] = []
        docs = list(state.get("retrieved_docs", []) or [])
        web_items = list(state.get("web_results", []) or [])
        legal_items = list(state.get("legal_results", []) or [])

        for tc in calls:
            t0 = time.time()
            if getattr(tc, "parse_error", ""):
                # 参数 JSON 解析失败（R1）：不执行，直接回灌错误消息提示 LLM 修正
                result = ToolResult(
                    tool=tc.name,
                    call_id=tc.id,
                    ok=False,
                    summary=f"参数解析失败: {tc.parse_error}",
                    data={},
                )
            else:
                result = registry.execute(tc.name, tc.arguments or {}, call_id=tc.id)

            tool_results.append(result)
            tool_messages.append(result.to_tool_message())
            log_entries.append({
                "tool": tc.name,
                "arguments": tc.arguments or {},
                "ok": result.ok,
                "summary": result.summary,
                "source": result.source,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "turn": state.get("agent_turns", 0) or 0,
            })
            if not result.ok:
                continue
            if result.source == SOURCE_INTERNAL_KB and result.data.get("docs"):
                docs = _merge_docs(docs, result.data["docs"])
            elif result.source == SOURCE_WEB and result.data.get("results"):
                web_items = _merge_by_url(web_items, result.data["results"])
            elif result.source == SOURCE_LEGAL and result.data.get("results"):
                legal_items = _merge_by_url(legal_items, result.data["results"])

        return {
            "tool_calls": [],                 # 消费后清空
            "tool_results": tool_results,
            "tool_log": list(state.get("tool_log", []) or []) + log_entries,
            "messages": tool_messages,
            "retrieved_docs": docs,
            "web_results": web_items,
            "legal_results": legal_items,
        }

    def route_after_agent(state: AgentState) -> str:
        """agent → tools（有 tool_calls）| validate（无 tool_calls）。

        轮数上限由 agent_node 在达到上限时移除 tools 强制产出答案，故此处
        只需判断是否有待执行工具调用即可保证终止（REQ-UW4 兜底见 agent_node）。
        """
        if state.get("tool_calls"):
            return "tools"
        return "final"

    def route_after_tools(state: AgentState) -> str:
        """tools → agent（继续 ReAct 循环）。"""
        return "agent"

    return {
        "agent": agent_node,
        "tools": tools_node,
        "route_after_agent": route_after_agent,
        "route_after_tools": route_after_tools,
    }
