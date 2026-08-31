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
from src.llm.base import ToolCall

logger = logging.getLogger(__name__)


# DeepSeek 在无 tools 参数时仍可能以纯文本输出 DSML 工具调用语法
# （达到轮数上限被强制作答时），兜底清除保证最终答案是自然语言。
_DSML_TOOL_CALLS_RE = re.compile(r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>", re.DOTALL)

# 强制作答轮模型仍可能输出"让我进一步检索……"式过渡语而非答案
# （历史案例：输出一句检索计划被当成最终答案推给用户）。检测模式：
# 短文本 + 意图动词，且不含任何法条引用特征。
_TRANSITION_RE = re.compile(r"(让我|我来|我将|需要|再|先)(进一步)?(检索|查询|搜索|调用|查看|获取|核实)")


def _strip_dsml_tool_calls(text: str) -> str:
    """清除文本中的 DSML 工具调用块（轮数上限强制作答的兜底清洗）。"""
    if not text or "DSML" not in text:
        return text
    return _DSML_TOOL_CALLS_RE.sub("", text).strip()


def _looks_like_transition(text: str) -> bool:
    """是否为"准备去做某事"的过渡语而非答案（仅用于强制作答轮兜底）。"""
    if not text or len(text) > 120:
        return False
    return bool(_TRANSITION_RE.search(text))


# 强制作答轮检测到过渡语时的重试指令（追加在消息末尾，再调一次 LLM）
_FORCED_FINAL_ANSWER_PROMPT = (
    "你上一条回复是『准备去做某事』的过渡语，不是对用户问题的回答，且现在已没有工具可用。"
    "请立即输出面向用户的最终法律解答：直接给出结论、法律依据（引用你已检索到的法条名称与条款号）"
    "与简要分析。若确有个别信息缺口，基于现有证据回答并说明局限。"
    "禁止再输出『让我检索』『我将查询』等任何过渡语或后续计划。"
)


# ---------------------------------------------------------------------------
# 消息转换工具
# ---------------------------------------------------------------------------


def _tool_calls_to_openai(tool_calls: list[ToolCall | dict]) -> list[dict]:
    """tool_call → OpenAI 格式（assistant 消息回灌，共享约定 §8.4）。

    两种**活**形态（删改前先追数据流，勿凭调用点推断）：
    ① ToolCall dataclass —— agent_node 处理本轮 LLM 决策结果；
    ② LangChain dict {"name","args","id","type"} —— state.messages 经
       add_messages reducer 转成 AIMessage 后，_messages_to_dicts 重建
       历史消息时读出的 tool_calls 即此形态。
    （OpenAI 原始 {"function": ...} dict 形态无生产路径：dict 消息在
    _messages_to_dicts 里原样透传，不会进本函数。）
    """
    result: list[dict] = []
    for tc in tool_calls or []:
        if isinstance(tc, dict):
            result.append(
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args", tc.get("arguments", {})), ensure_ascii=False),
                    },
                }
            )
        else:
            result.append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
            )
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
    seen = {(d.get("law_name", ""), d.get("article_range", ""), (d.get("content") or "")[:60]) for d in existing}
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
        key = r.get("url") or r.get("title") or ""
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
            # 明确告知模型不能再调工具：不仅禁 DSML 语法，还要禁"过渡语"——
            # 历史案例：模型被强制作答时输出"让我进一步检索……"一句空话，
            # 被当成最终答案推给用户
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "你已达到工具调用轮数上限，无法再调用任何工具。请立即基于已获取的工具结果，"
                        "输出面向用户的最终法律解答（结论 + 法条依据 + 简要分析）。"
                        "禁止输出'让我进一步检索'之类的过渡语或后续计划；若信息不足，"
                        "基于现有证据回答并明确说明局限。"
                    ),
                }
            )
        update: dict[str, Any] = {
            "agent_turns": turns + 1,
            "tool_results": [],
        }
        try:
            # 必须经 LLMBackend 公开入口 chat_with_tools（内部已是 LangChain
            # bind_tools + invoke，D-M3-13）：D-M1-3 的重试语义与 FailoverLLMBackend
            # 的 4xx 运行期降级语义都实现于该入口链路上，直接
            # `chat_model.bind_tools().invoke()` 会同时绕过这两层——瞬时 429/5xx
            # 不再重试、主后端 4xx 不再降级 Ollama。预算埋点由 ChatModel 挂载的
            # LLMBudgetCallbackHandler 负责（F14），走哪个入口都会计数。
            resp = llm.chat_with_tools(messages, schemas, tool_choice="auto")
        except Exception as e:
            logger.error(f"agent 节点 LLM 工具调用失败: {e}", exc_info=True)
            update["tool_calls"] = []
            update["answer"] = "抱歉，模型调用失败，暂时无法回答该问题。"
            update["messages"] = [{"role": "assistant", "content": update["answer"]}]
            return update

        # 防御性兜底：过滤 name 为空的 ToolCall（DeepSeek V4 想直接回答时
        # 可能返回函数名为空的占位 tool_call，不应路由到 tools）。
        calls: list[ToolCall] = [tc for tc in (resp.tool_calls or []) if getattr(tc, "name", "")]
        if calls and schemas:
            update["tool_calls"] = calls
            update["messages"] = [
                {
                    "role": "assistant",
                    "content": resp.content or "",
                    "tool_calls": _tool_calls_to_openai(calls),
                }
            ]
        else:
            # 无工具可用 / 模型未返回工具调用 → content 即最终答案
            answer = _strip_dsml_tool_calls((resp.content or "").strip())
            # 强制作答轮兜底：模型仍输出过渡语而非答案时，追加明确指令重试一次。
            # 仅在强制作答轮触发（schemas 为空）；正常轮的过渡语随 tool_calls 走，不受影响。
            if not schemas and _looks_like_transition(answer):
                logger.warning(f"强制作答轮输出过渡语，重试一次: {answer[:60]}")
                messages.append({"role": "assistant", "content": answer})
                messages.append({"role": "system", "content": _FORCED_FINAL_ANSWER_PROMPT})
                try:
                    resp2 = llm.chat_with_tools(messages, [])
                    retry = _strip_dsml_tool_calls((resp2.content or "").strip())
                    if retry and len(retry) > len(answer):
                        answer = retry
                except Exception as e:
                    logger.warning(f"强制作答重试失败，沿用原输出: {e}")
            answer = answer or "抱歉，暂时无法回答该问题。"
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
            result = registry.execute(tc.name, tc.arguments or {}, call_id=tc.id)

            tool_results.append(result)
            tool_messages.append(result.to_tool_message())
            log_entries.append(
                {
                    "tool": tc.name,
                    "arguments": tc.arguments or {},
                    "ok": result.ok,
                    "summary": result.summary,
                    "source": result.source,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "turn": state.get("agent_turns", 0) or 0,
                }
            )
            if not result.ok:
                continue
            if result.source == SOURCE_INTERNAL_KB and result.data.get("docs"):
                docs = _merge_docs(docs, result.data["docs"])
            elif result.source == SOURCE_WEB and result.data.get("results"):
                web_items = _merge_by_url(web_items, result.data["results"])
            elif result.source == SOURCE_LEGAL and result.data.get("results"):
                legal_items = _merge_by_url(legal_items, result.data["results"])

        return {
            "tool_calls": [],  # 消费后清空
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
