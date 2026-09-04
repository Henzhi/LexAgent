"""
孤儿流回收测试（2026-09-04，修复「切换对话 / 刷新页面后 Token 白烧、前端空白」）。

背景：D-M3-12 的语义是「被动断线后生成继续跑完并写事件日志，等前端按 seq 游标
重连补发」。但前端并非每次断线都会重连——用户刷新后没回来、直接关标签页、
切走会话后不再关注时，后端仍会把整轮 LLM 烧完，产出无人接收。

本模块覆盖新增的孤儿宽限机制：断线后给流打 deadline，resume 到达即认领
（清除 deadline，生成继续跑完），超时无人认领则立即停止生成。

全程离线：日志走进程内模式，生成器用内存 fake（同 test_stream_resume.py）。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest

from src.observability.stream_log import get_stream_log, reset_stream_log


@pytest.fixture(autouse=True)
def _memory_mode(monkeypatch):
    monkeypatch.setattr("src.config.REDIS_URL", "")
    reset_stream_log()
    yield
    reset_stream_log()


def _fake_factory(events, consumed=None, interval=0.05):
    def factory():
        for e in events:
            if consumed is not None:
                consumed.append(e["content"])
            time.sleep(interval)
            yield dict(e)

    return factory


def _drive(agen, stop_after=0, trigger=None):
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        evs = log.read_after(stream_id, 0)
        if evs and evs[-1].get("type") == "__stream_end__":
            return evs
        time.sleep(0.05)
    return log.read_after(stream_id, 0)


# ---------------------------------------------------------------------------
# 孤儿登记表（单元）
# ---------------------------------------------------------------------------


class TestOrphanRegistry:
    def test_expires_after_grace(self, monkeypatch):
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0.05)
        routes._mark_orphan("r1")
        assert routes._orphan_expired("r1") is False
        time.sleep(0.1)
        assert routes._orphan_expired("r1") is True

    def test_claim_clears_deadline(self, monkeypatch):
        """重连认领后不再判定为孤儿，生成得以继续跑完。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0.05)
        routes._mark_orphan("r1")
        routes._claim_stream("r1")
        time.sleep(0.1)
        assert routes._orphan_expired("r1") is False

    def test_grace_zero_keeps_legacy_behavior(self, monkeypatch):
        """配置关闭回收（0）→ 退回旧行为：断线后一律跑完。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0)
        routes._mark_orphan("r1")
        time.sleep(0.1)
        assert routes._orphan_expired("r1") is False

    def test_expired_only_for_known_stream(self):
        from src.api import routes

        assert routes._orphan_expired("") is False
        assert routes._orphan_expired("unknown") is False


# ---------------------------------------------------------------------------
# 桥接层退出语义（集成）
# ---------------------------------------------------------------------------


class TestOrphanReclaim:
    def test_unclaimed_stream_stops_after_grace(self, monkeypatch):
        """断线后无人重连：宽限期一到立即停，不再把整轮烧完（省 Token）。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0.3)
        log = get_stream_log()
        events = [{"type": "token", "content": str(i)} for i in range(40)]
        consumed: list[str] = []
        de = asyncio.Event()

        _drive(
            routes._bridge_sync_stream(
                _fake_factory(events, consumed=consumed, interval=0.05),
                object(),
                de,
                None,
                stream_id="r1",
            ),
            stop_after=2,
            trigger=de.set,
        )
        evs = _wait_end_marker(log, "r1", timeout=5.0)
        tokens = [e for e in evs if e["type"] == "token"]
        # 40 × 0.05s ≈ 2s；断线发生在 ~0.1s，宽限 0.3s → 应在 ~0.4s 停下
        assert len(tokens) < 20, f"孤儿流未被回收，仍烧了 {len(tokens)}/40 个事件"
        assert evs[-1]["type"] == "__stream_end__", "停止时也要写终局标记，重连方不会空等"

    def test_claimed_stream_runs_to_completion(self, monkeypatch):
        """宽限期内被 resume 认领 → 生成继续跑完，重连方能补到完整内容。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0.6)
        log = get_stream_log()
        events = [{"type": "token", "content": str(i)} for i in range(20)]
        consumed: list[str] = []
        de = asyncio.Event()

        def trigger():
            de.set()
            # 模拟"断线后不久前端重连"：认领发生在宽限期内
            threading.Timer(0.25, lambda: routes._claim_stream("r1")).start()

        _drive(
            routes._bridge_sync_stream(
                _fake_factory(events, consumed=consumed, interval=0.05),
                object(),
                de,
                None,
                stream_id="r1",
            ),
            stop_after=2,
            trigger=trigger,
        )
        evs = _wait_end_marker(log, "r1", timeout=5.0)
        tokens = [e for e in evs if e["type"] == "token"]
        assert len(tokens) == 20, "已被认领的流必须跑完，否则重连方拿不到完整答案"

    def test_cancel_still_stops_immediately(self, monkeypatch):
        """主动取消语义不变：不等宽限期，立即停。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 30)
        events = [{"type": "token", "content": str(i)} for i in range(40)]
        consumed: list[str] = []
        ce = threading.Event()

        _drive(
            routes._bridge_sync_stream(
                _fake_factory(events, consumed=consumed, interval=0.02),
                object(),
                None,
                ce,
                stream_id="r1",
            ),
            stop_after=2,
            trigger=ce.set,
        )
        time.sleep(0.3)
        assert len(consumed) < 30

    def test_orphan_registry_cleaned_up(self, monkeypatch):
        """流终局后不留孤儿登记，避免注册表无限膨胀。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0.3)
        de = asyncio.Event()
        _drive(
            routes._bridge_sync_stream(
                _fake_factory([{"type": "token", "content": str(i)} for i in range(10)], interval=0.02),
                object(),
                de,
                None,
                stream_id="r1",
            ),
            stop_after=1,
            trigger=de.set,
        )
        _wait_end_marker(get_stream_log(), "r1", timeout=5.0)
        assert "r1" not in routes._ORPHAN_DEADLINES
        assert "r1" not in routes._ACTIVE_STREAMS


# ---------------------------------------------------------------------------
# resume 认领
# ---------------------------------------------------------------------------


class TestResumeClaimsStream:
    @pytest.fixture(autouse=True)
    def _auth_ok(self, client):
        from src.api.auth import require_registered_user
        from src.api.main import app

        app.dependency_overrides[require_registered_user] = lambda: "test-user"
        yield
        app.dependency_overrides.pop(require_registered_user, None)

    def test_resume_claims_and_clears_deadline(self, client, monkeypatch):
        """resume 到达即认领：孤儿 deadline 被清除（生成继续跑完）。"""
        from src.api import routes

        monkeypatch.setattr(routes, "STREAM_ORPHAN_GRACE_SECONDS", 0.3)
        log = get_stream_log()
        log.append("rX", {"type": "token", "content": "半句"})
        routes._mark_orphan("rX")

        r = client.get("/api/chat/stream/resume", params={"request_id": "rX", "after_seq": 0})
        assert r.status_code == 200
        assert "半句" in r.text
        # 重放完即止（生成不在进行），但认领必须已发生
        assert routes._orphan_expired("rX") is False
