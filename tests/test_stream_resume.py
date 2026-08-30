"""
D-M3-12 断线重连测试：SSE 事件日志、桥接退出语义（被动断线跑完 / 主动取消
立即停 / 无日志立即停）、resume 重放接口。

全程离线：StreamEventLog 用进程内模式（monkeypatch REDIS_URL=""），
生成器用内存 fake。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest

from src.observability.stream_log import StreamEventLog, get_stream_log, reset_stream_log


@pytest.fixture(autouse=True)
def _memory_mode(monkeypatch):
    """bridge / resume 走进程内日志单例（确定性，不依赖本机 Redis 状态）。"""
    monkeypatch.setattr("src.config.REDIS_URL", "")
    reset_stream_log()
    yield
    reset_stream_log()


# ---------------------------------------------------------------------------
# StreamEventLog 单元行为
# ---------------------------------------------------------------------------


class TestStreamEventLog:
    def test_append_seq_increments(self):
        log = StreamEventLog(redis_url="")
        assert log.append("r1", {"type": "thinking"}) == 1
        assert log.append("r1", {"type": "token"}) == 2
        assert log.append("r2", {"type": "thinking"}) == 1  # 流间独立

    def test_read_after_filters(self):
        log = StreamEventLog(redis_url="")
        for i in range(4):
            log.append("r1", {"type": "token", "content": str(i)})
        evs = log.read_after("r1", 1)
        assert [e["seq"] for e in evs] == [2, 3, 4]
        assert log.read_after("r1", 99) == []

    def test_append_end_marker(self):
        log = StreamEventLog(redis_url="")
        log.append("r1", {"type": "token"})
        log.append_end("r1")
        evs = log.read_after("r1", 0)
        assert evs[-1]["type"] == "__stream_end__"

    def test_exists_and_expiry(self):
        log = StreamEventLog(redis_url="", ttl_seconds=60)
        log.append("r1", {"type": "thinking"})
        assert log.exists("r1") is True
        assert log.exists("r2") is False
        # 白盒：把过期时间拨到过去
        events, _ = log._memory["r1"]
        log._memory["r1"] = (events, time.monotonic() - 1)
        assert log.exists("r1") is False
        assert log.read_after("r1", 0) == []


# ---------------------------------------------------------------------------
# 桥接退出语义（D-M3-12 核心）
# ---------------------------------------------------------------------------


def _fake_factory(events, consumed=None, interval=0.03):
    def factory():
        for e in events:
            if consumed is not None:
                consumed.append(e["content"])
            time.sleep(interval)
            yield dict(e)

    return factory


def _drive(agen, stop_after=0, trigger=None):
    """消费桥接事件；返回实际收到的事件列表。"""

    async def _scenario():
        got = []
        it = agen.__aiter__()
        with contextlib.suppress(StopAsyncIteration):
            while True:
                item = await it.__anext__()
                got.append(item)
                if stop_after and len(got) >= stop_after:
                    if trigger is not None:
                        trigger()
        return got

    return asyncio.run(_scenario())


def _wait_end_marker(log, stream_id, timeout=5.0):
    """轮询等待 worker 写入终局标记（worker 在线程池里继续跑完）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        evs = log.read_after(stream_id, 0)
        if evs and evs[-1].get("type") == "__stream_end__":
            return evs
        time.sleep(0.05)
    return log.read_after(stream_id, 0)


class TestBridgeExitSemantics:
    def test_normal_finish_stamps_seq(self):
        """正常跑完：在线事件带 seq，日志完整（含终局标记）。"""
        from src.api.routes import _bridge_sync_stream

        events = [{"type": "token", "content": str(i)} for i in range(3)]
        got = _drive(_bridge_sync_stream(_fake_factory(events), object(), None, None, stream_id="r1"))
        assert [e["seq"] for e in got] == [1, 2, 3]
        evs = _wait_end_marker(get_stream_log(), "r1")
        assert evs[-1]["type"] == "__stream_end__"

    def test_passive_disconnect_keeps_running_and_logging(self):
        """被动断线：在线流停止，worker 继续跑完并写完整日志（D-M3-12 核心行为）。"""
        from src.api.routes import _bridge_sync_stream

        log = get_stream_log()
        events = [{"type": "token", "content": str(i)} for i in range(6)]
        de = asyncio.Event()
        trigger = de.set
        got = _drive(
            _bridge_sync_stream(_fake_factory(events, interval=0.05), object(), de, None, stream_id="r1"),
            stop_after=2,
            trigger=trigger,
        )
        assert len(got) <= 3  # 在线流很快终止
        evs = _wait_end_marker(log, "r1", timeout=5.0)
        # worker 跑完了全部事件（成本已沉没，重连可补发）
        tokens = [e for e in evs if e["type"] == "token"]
        assert len(tokens) == 6
        assert [e["seq"] for e in tokens] == [1, 2, 3, 4, 5, 6]
        assert evs[-1]["type"] == "__stream_end__"

    def test_cancel_stops_immediately(self):
        """主动取消：worker 立即停，不跑完（省 Token，现状不变）。"""
        from src.api.routes import _bridge_sync_stream

        log = get_stream_log()
        events = [{"type": "token", "content": str(i)} for i in range(30)]
        consumed: list[str] = []
        ce = threading.Event()
        trigger = ce.set
        _drive(
            _bridge_sync_stream(
                _fake_factory(events, consumed=consumed, interval=0.02), object(), None, ce, stream_id="r1"
            ),
            stop_after=2,
            trigger=trigger,
        )
        time.sleep(0.3)  # 给 worker 收尾时间
        assert len(consumed) < 30, "取消后必须立即停止生成"
        evs = log.read_after("r1", 0)
        assert evs[-1]["type"] == "__stream_end__"  # 终局标记让重连方不会无限等

    def test_disconnect_without_log_stops_immediately(self):
        """未带 request_id（无日志可补发）→ 保持旧行为立即停。"""
        from src.api.routes import _bridge_sync_stream

        events = [{"type": "token", "content": str(i)} for i in range(30)]
        consumed: list[str] = []
        de = asyncio.Event()
        trigger = de.set
        _drive(
            _bridge_sync_stream(
                _fake_factory(events, consumed=consumed, interval=0.02), object(), de, None, stream_id=""
            ),
            stop_after=2,
            trigger=trigger,
        )
        time.sleep(0.3)
        assert len(consumed) < 30, "无日志可补发时应立即停止（旧行为）"

    def test_worker_registers_and_deregisters_active_stream(self):
        from src.api.routes import _ACTIVE_STREAMS, _bridge_sync_stream

        events = [{"type": "token", "content": "x"}]
        _drive(_bridge_sync_stream(_fake_factory(events), object(), None, None, stream_id="r1"))
        assert "r1" not in _ACTIVE_STREAMS


# ---------------------------------------------------------------------------
# GET /api/chat/stream/resume
# ---------------------------------------------------------------------------


class TestResumeEndpoint:
    def test_400_without_request_id(self, client):
        assert client.get("/api/chat/stream/resume").status_code == 400

    def test_404_unknown_stream(self, client):
        r = client.get("/api/chat/stream/resume", params={"request_id": "nope", "after_seq": 0})
        assert r.status_code == 404

    def test_replay_and_done(self, client):
        """重放 after_seq 之后的事件并以 [DONE] 结尾（生成已不在进行 → 重放完即止）。"""
        from src.api.routes import _ACTIVE_STREAMS

        log = get_stream_log()
        for i in range(3):
            log.append("rX", {"type": "token", "content": f"第{i}段"})
        log.append_end("rX")
        _ACTIVE_STREAMS.pop("rX", None)  # 生成已不在进行

        r = client.get("/api/chat/stream/resume", params={"request_id": "rX", "after_seq": 1})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = r.text
        assert "第0段" not in body  # after_seq=1：第一段不重放
        assert "第1段" in body and "第2段" in body
        assert "data: [DONE]" in body
