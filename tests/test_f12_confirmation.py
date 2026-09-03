"""
F12 v1 人工确认测试（D-M3-9a，路径 A：进入图之前的一次确认）。

覆盖技术方案 §4 实施步骤 6 的四条验收 + 风险 R7（换题重新确认）：
- A 类不受影响（不产出确认事件，正常回答）
- B 类未确认 → 产出 confirmation_required 并结束流（零 LLM 调用）
- 确认后正常执行
- 标记存储失败回落 A 类（fail-open）
外加：ConfirmationStore 单元行为（TTL 过期 / 换题比对 / 取消）、
ask() 双路径口径、/api/chat/confirm 接口校验。

全程离线：ConfirmationStore 用进程内模式（redis_url=""），LLM 用 FakeToolLLM。
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeRetriever, FakeToolLLM
from src.agents.graph import LawAgentGraph
from src.agents.tools import build_default_tools
from src.memory.confirmation_store import ConfirmationStore, reset_confirmation_store
from src.rag.scenes import KIND_B, classify_scene

# 自校验查询：若场景关键词调整导致归类变化，本文件测试会第一时间暴露
B_QUERY = "帮我起草一份房屋租赁合同"  # → contract_draft（B 类）
A_QUERY = "行政拘留最长多久"  # → A 类


def _final_response(text="已生成的最终答案") -> object:
    from src.llm.base import ToolCallResponse

    return ToolCallResponse(content=text, tool_calls=[], raw={})


def _build_agent(llm, store: ConfirmationStore) -> LawAgentGraph:
    retriever = FakeRetriever()
    return LawAgentGraph(
        retriever=retriever,
        llm=llm,
        top_k=3,
        max_retries=0,
        memory_manager=None,
        faq_cache=None,
        query_logger=None,
        registry=build_default_tools(retriever),
        confirmation_store=store,
    )


@pytest.fixture
def store() -> ConfirmationStore:
    """进程内模式的独立存储（不打 Redis，测试间隔离）。"""
    return ConfirmationStore(redis_url="")


@pytest.fixture(autouse=True)
def _scene_guard():
    """查询归类自检：场景清单调整导致 B/A 归类变化时立刻暴露。"""
    assert classify_scene(B_QUERY).kind == KIND_B
    assert classify_scene(A_QUERY).kind != KIND_B
    yield


# ---------------------------------------------------------------------------
# ConfirmationStore 单元行为
# ---------------------------------------------------------------------------


class TestConfirmationStore:
    def test_confirm_then_check(self, store):
        assert store.is_confirmed("", "s1", B_QUERY) is False
        assert store.confirm("", "s1", B_QUERY) is True
        assert store.is_confirmed("", "s1", B_QUERY) is True

    def test_session_isolation(self, store):
        store.confirm("", "s1", B_QUERY)
        assert store.is_confirmed("", "s1", B_QUERY) is True
        assert store.is_confirmed("", "s2", B_QUERY) is False

    def test_query_mismatch_requires_reconfirm(self, store):
        """R7：确认后的 query 与当前不一致 → 视为未确认（防确认后换题绕过）。"""
        store.confirm("", "s1", "起草租赁合同")
        assert store.is_confirmed("", "s1", "起草买卖合同") is False

    def test_cancel_clears_mark(self, store):
        store.confirm("", "s1", B_QUERY)
        store.clear("", "s1")
        assert store.is_confirmed("", "s1", B_QUERY) is False

    def test_ttl_expiry(self, store):
        """TTL 过期后标记失效（Q7：超时取消，需重新发起确认）。"""
        import time

        store.confirm("", "s1", B_QUERY)
        scope = "anon:s1"
        stored_q, _ = store._memory[scope]
        store._memory[scope] = (stored_q, time.monotonic() - 1)  # 置为已过期
        assert store.is_confirmed("", "s1", B_QUERY) is False

    def test_read_failure_fails_open(self, store):
        """存储异常 fail-open：视为已确认，B 类回落 A 类直接执行（主链路不被确认机制阻断）。"""
        store.confirm("", "s1", B_QUERY)

        class _BrokenClient:
            def get(self, key):
                raise RuntimeError("redis down")

            def setex(self, key, ttl, value):
                raise RuntimeError("redis down")

            def delete(self, key):
                raise RuntimeError("redis down")

        store._client = _BrokenClient()
        assert store.is_confirmed("", "s1", B_QUERY) is True

    def test_write_failure_falls_back_to_memory(self, store):
        """Redis 写失败退化进程内存储，确认流程仍然可用（D-M3-8 同款降级）。"""

        class _BrokenClient:
            def get(self, key):
                raise RuntimeError("redis down")

            def setex(self, key, ttl, value):
                raise RuntimeError("redis down")

            def delete(self, key):
                raise RuntimeError("redis down")

        store._client = _BrokenClient()
        assert store.confirm("", "s1", B_QUERY) is True
        store._client = None  # 回退后用进程内数据校验
        assert store.is_confirmed("", "s1", B_QUERY) is True


# ---------------------------------------------------------------------------
# graph.stream()：前置确认分支
# ---------------------------------------------------------------------------


class TestStreamConfirmation:
    def test_class_a_unaffected(self, store):
        """A 类不受影响：无确认事件，正常出答案（验收 1）。"""
        llm = FakeToolLLM([_final_response()])
        events = list(_build_agent(llm, store).stream(A_QUERY, session_id="s1"))
        assert not any(e["type"] == "confirmation_required" for e in events)
        assert any(e["type"] == "token" for e in events)

    def test_class_b_unconfirmed_gates_before_any_llm_call(self, store):
        """B 类未确认：产出 confirmation_required 并结束流，零 LLM/工具消耗（验收 2）。"""
        llm = FakeToolLLM([_final_response()])
        events = list(_build_agent(llm, store).stream(B_QUERY, session_id="s1"))

        gate_events = [e for e in events if e["type"] == "confirmation_required"]
        assert len(gate_events) == 1
        payload = gate_events[0]
        assert payload["scene"] == "contract_draft"
        assert payload["scene_name"]
        assert payload["prompt"]
        assert payload["options"] == ["确认", "取消"]
        assert payload["confirm_id"] == "anon:s1:contract_draft"
        # 确认分支必须终止流：不允许再出现 token / meta / 工具事件
        rest = [e["type"] for e in events if e["type"] != "confirmation_required"]
        assert "token" not in rest and "meta" not in rest and "tool_call" not in rest
        # 零消耗：确认发生在任何 LLM 调用之前（D-M3-9a 的核心收益）
        assert llm.calls == []

    def test_confirmed_then_executes(self, store):
        """确认后重新发起 → 正常执行（验收 3）。"""
        llm = FakeToolLLM([_final_response()])
        store.confirm("", "s1", B_QUERY)
        events = list(_build_agent(llm, store).stream(B_QUERY, session_id="s1"))
        assert not any(e["type"] == "confirmation_required" for e in events)
        assert any(e["type"] == "token" for e in events)

    def test_different_query_after_confirm_re_gates(self, store):
        """R7：确认了 A 问题却发来 B 问题 → 重新要求确认。"""
        llm = FakeToolLLM([_final_response()])
        store.confirm("", "s1", "另一个问题")
        events = list(_build_agent(llm, store).stream(B_QUERY, session_id="s1"))
        assert any(e["type"] == "confirmation_required" for e in events)

    def test_session_scope(self, store):
        """确认标记按 session 隔离：s1 已确认不影响 s2。"""
        llm = FakeToolLLM([_final_response(), _final_response()])
        agent = _build_agent(llm, store)
        store.confirm("", "s1", B_QUERY)
        assert not any(e["type"] == "confirmation_required" for e in agent.stream(B_QUERY, session_id="s1"))
        assert any(e["type"] == "confirmation_required" for e in agent.stream(B_QUERY, session_id="s2"))


# ---------------------------------------------------------------------------
# graph.ask()：非流式路径同口径
# ---------------------------------------------------------------------------


class TestAskConfirmation:
    def test_ask_unconfirmed_returns_payload(self, store):
        """ask() 双路径口径一致：B 类未确认 → confirmation_required 载荷，不进图。"""
        llm = FakeToolLLM([_final_response()])
        result = _build_agent(llm, store).ask(B_QUERY, session_id="s1")
        assert result["answer"] == ""
        assert result["confirmation_required"]["scene"] == "contract_draft"
        assert llm.calls == []

    def test_ask_confirmed_executes(self, store):
        llm = FakeToolLLM([_final_response()])
        store.confirm("", "s1", B_QUERY)
        result = _build_agent(llm, store).ask(B_QUERY, session_id="s1")
        assert "confirmation_required" not in result
        assert result["answer"] == "已生成的最终答案"

    def test_ask_storage_failure_fails_open(self, store):
        """验收 4：标记存储失败 → 回落 A 类直接执行。"""

        class _BrokenClient:
            def __getattr__(self, name):
                raise RuntimeError("redis down")

        store._client = _BrokenClient()
        llm = FakeToolLLM([_final_response()])
        result = _build_agent(llm, store).ask(B_QUERY, session_id="s1")
        assert "confirmation_required" not in result
        assert result["answer"] == "已生成的最终答案"


# ---------------------------------------------------------------------------
# /api/chat/confirm 接口
# ---------------------------------------------------------------------------


class _FakeAgent:
    """接口级假 Agent：确认后续跑只产出思考 + token，不触碰真实 LLM。"""

    def __init__(self, text="确认后生成的最终答案"):
        self.text = text

    def stream(self, query, history=None, session_id=""):
        yield {"type": "thinking", "content": "确认后开始执行"}
        yield {"type": "token", "content": self.text}


class TestConfirmEndpoint:
    @pytest.fixture(autouse=True)
    def _memory_mode_store(self, monkeypatch):
        """接口测试用进程内单例（确定性，不依赖本机 Redis 状态）。"""
        monkeypatch.setattr("src.config.REDIS_URL", "")
        reset_confirmation_store()
        # 确认后续跑用假 Agent，避免打真实 DeepSeek/Ollama
        monkeypatch.setattr("src.api.routes.get_agent", lambda: _FakeAgent())
        # 预算熔断前置检查置空：避免本机 .env 的 BUDGET_* 让测试结果漂移
        monkeypatch.setattr("src.api.routes._budget_block_message", lambda: "")
        yield
        reset_confirmation_store()

    def test_rejects_unknown_scene(self, client):
        r = client.post(
            "/api/chat/confirm",
            json={"session_id": "s1", "scene_id": "not_a_scene", "query": B_QUERY},
        )
        assert r.status_code == 400

    def test_rejects_class_a_scene(self, client):
        """仅接受 B 类场景 id（A 类不需要确认，防误用）。"""
        from src.rag.scenes import scene_ids

        a_scene = next(s for s in scene_ids() if classify_scene("查询劳动合同法第四十六条").scene_id == s)
        r = client.post(
            "/api/chat/confirm",
            json={"session_id": "s1", "scene_id": a_scene, "query": "查询劳动合同法第四十六条"},
        )
        assert r.status_code == 400

    def test_approve_sets_mark_and_streams_execution(self, client):
        """approved=True：写入标记 + 在同一 SSE 连接上直接续跑生成（不用再发一次 stream）。"""
        from src.memory.confirmation_store import get_confirmation_store

        r = client.post(
            "/api/chat/confirm",
            json={
                "session_id": "s1",
                "scene_id": "contract_draft",
                "query": B_QUERY,
                "approved": True,
                "history": [],
                "request_id": "req_confirm_test",
            },
        )
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = r.text
        # 标记已写入（旧客户端随后重发 /chat/stream 也能直接执行）
        assert get_confirmation_store().is_confirmed("", "s1", B_QUERY) is True
        # 同一连接直接产出生成事件，无需第二次请求
        assert "确认后生成的最终答案" in body
        assert "data: [DONE]" in body

    def test_approve_stream_still_gated_after_marker(self, client):
        """确认后续跑的流是 Agent 事件流，不含 confirmation_required（不会再次要求确认）。"""
        r = client.post(
            "/api/chat/confirm",
            json={
                "session_id": "s1",
                "scene_id": "contract_draft",
                "query": B_QUERY,
                "approved": True,
                "request_id": "req_confirm_test2",
            },
        )
        assert r.status_code == 200
        assert "confirmation_required" not in r.text

    def test_cancel_clears_mark(self, client):
        """approved=False 保持 JSON 语义：仅清除标记，不启动续跑。"""
        from src.memory.confirmation_store import get_confirmation_store

        # 先确认（approved=True 为 SSE 流，消费其响应体以触发完整生成）
        r = client.post(
            "/api/chat/confirm",
            json={"session_id": "s1", "scene_id": "contract_draft", "query": B_QUERY, "approved": True},
        )
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert get_confirmation_store().is_confirmed("", "s1", B_QUERY) is True
        # 再取消 → 标记清除，返回 JSON
        r2 = client.post(
            "/api/chat/confirm",
            json={"session_id": "s1", "scene_id": "contract_draft", "query": B_QUERY, "approved": False},
        )
        assert r2.status_code == 200
        assert r2.json()["ok"] is True
        assert get_confirmation_store().is_confirmed("", "s1", B_QUERY) is False
