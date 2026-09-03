"""/api/health 降级可观测性守护测试（2026-09-03 审查整改）。

问题：主后端一次瞬时 401/403 就会触发 failover 切到 Ollama 备用后端，而
/health 此前恒定返回 status=ok——服务悄悄跑在降级态，运维完全无感（审查报告
「长期项：降级链路可观测化」）。

本文件守两条契约：
1. /health 必须暴露 degraded / degraded_reason / active_backend / budget_exceeded；
2. 观测字段的读取失败必须 fail-open（不能把健康检查打成 503，否则负载均衡会
   摘掉所有实例——信息缺失远好于健康检查挂掉）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def health_client():
    from src.api.main import app

    return TestClient(app)


class TestHealthExposesDegradedState:
    """降级态必须能从 /health 读出来。"""

    def test_degraded_state_surfaced(self, health_client, monkeypatch):
        """降级时 /health 返回 degraded=True + 原因 + 实际生效后端。"""
        monkeypatch.setattr(
            "src.api.routes._llm_degraded_state",
            lambda: (True, "主后端调用失败: AuthenticationError: 401", "ollama"),
        )
        resp = health_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["degraded"] is True
        assert "401" in data["degraded_reason"]
        assert data["active_backend"] == "ollama"

    def test_healthy_state_reports_not_degraded(self, health_client, monkeypatch):
        """未降级时 degraded=False、reason 为空、后端标签为 openai。"""
        monkeypatch.setattr("src.api.routes._llm_degraded_state", lambda: (False, "", "openai"))
        data = health_client.get("/api/health").json()
        assert data["degraded"] is False
        assert data["degraded_reason"] == ""
        assert data["active_backend"] == "openai"

    def test_budget_exceeded_surfaced(self, health_client, monkeypatch):
        monkeypatch.setattr("src.api.routes._budget_exceeded_flag", lambda: True)
        assert health_client.get("/api/health").json()["budget_exceeded"] is True

    def test_response_has_all_legacy_fields(self, health_client):
        """新增字段不能挤掉老字段（老消费方必须不受影响）。"""
        data = health_client.get("/api/health").json()
        for key in ("status", "version", "index_ready", "doc_count", "llm_model"):
            assert key in data


class TestObservationFailsOpen:
    """观测字段读取失败不得影响健康检查本身。"""

    def test_degraded_state_helper_swallows_errors(self, monkeypatch):
        """_llm_degraded_state 自身抛异常时返回 (False, '', '')。"""
        from src.api import routes

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(routes, "get_llm", _boom)
        assert routes._llm_degraded_state() == (False, "", "")

    def test_budget_flag_helper_swallows_errors(self, monkeypatch):
        """_budget_exceeded_flag 自身抛异常时返回 False。"""
        from src.api import routes

        def _boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr(routes, "get_budget", _boom)
        assert routes._budget_exceeded_flag() is False

    def test_health_endpoint_still_200_when_llm_broken(self, health_client, monkeypatch):
        """get_llm 整条链路炸了，健康检查也必须 200（不能 503 摘实例）。"""
        from src.api import routes

        def _boom():
            raise RuntimeError("llm down")

        monkeypatch.setattr(routes, "get_llm", _boom)
        resp = health_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["degraded"] is False


class TestFailoverDegradedReason:
    """failover 必须记录降级原因，且回切后清空。"""

    def _failover(self, monkeypatch):
        from src.llm.failover import FailoverLLMBackend

        primary = MagicMock()
        primary.model = "deepseek-v4-flash"
        primary.temperature = 0.1
        primary.top_p = 0.9
        primary.max_tokens = 1024
        primary.max_retries = 1
        primary.retry_delay = 2.0
        fallback = MagicMock()
        fallback.model = "qwen2.5:7b"
        fallback.temperature = 0.1
        fallback.top_p = 0.9
        fallback.max_tokens = 1024
        fallback.max_retries = 1
        fallback.retry_delay = 2.0
        return FailoverLLMBackend(primary=primary, fallback=fallback)

    def test_mark_degraded_records_reason(self, monkeypatch):
        f = self._failover(monkeypatch)
        assert f.degraded is False
        assert f.degraded_reason == ""
        f.mark_degraded("主后端创建失败: missing api key")
        assert f.degraded is True
        assert "missing api key" in f.degraded_reason

    def test_switch_to_fallback_records_exception(self, monkeypatch):
        f = self._failover(monkeypatch)
        f._switch_to_fallback(RuntimeError("401 Unauthorized"))
        assert f.degraded is True
        assert "401 Unauthorized" in f.degraded_reason

    def test_recover_clears_reason(self, monkeypatch):
        f = self._failover(monkeypatch)
        f._switch_to_fallback(RuntimeError("boom"))
        f._recover()
        assert f.degraded is False
        assert f.degraded_reason == ""

    def test_missing_primary_has_reason(self, monkeypatch):
        from src.llm.failover import FailoverLLMBackend

        fallback = MagicMock()
        fallback.model = "qwen2.5:7b"
        fallback.temperature = 0.1
        fallback.top_p = 0.9
        fallback.max_tokens = 1024
        fallback.max_retries = 1
        fallback.retry_delay = 2.0
        f = FailoverLLMBackend(primary=None, fallback=fallback)
        assert f.degraded is True
        assert f.degraded_reason != ""


class TestLLMAdapterPassthrough:
    """适配器要把降级三元组透出（非 failover 后端也要有稳定标签）。"""

    def _adapter_with(self, **attrs):
        """构造 LLMAdapter；backend 只需 model/temperature，其余属性按需显式设置。"""
        from src.llm.adapter import LLMAdapter

        backend = type("_Backend", (), {"model": "deepseek-v4-flash", "temperature": 0.1})()
        for k, v in attrs.items():
            setattr(backend, k, v)
        return LLMAdapter(backend)

    def test_adapter_exposes_failover_state(self):
        adapter = self._adapter_with(degraded=True, degraded_reason="401", active_backend="ollama")
        assert adapter.degraded is True
        assert adapter.degraded_reason == "401"
        assert adapter.active_backend == "ollama"

    def test_adapter_defaults_for_plain_backend(self):
        """非 failover 后端：降级字段取安全默认值，active_backend 有稳定标签。"""
        adapter = self._adapter_with()
        assert adapter.degraded is False
        assert adapter.degraded_reason == ""
        assert adapter.active_backend in ("openai", "ollama")
