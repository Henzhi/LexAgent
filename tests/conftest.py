"""pytest fixtures — mock LLM/Ollama，避免依赖外部服务"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """返回 FastAPI TestClient，直接测试路由"""
    from src.api.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    """确保测试环境使用正确的配置"""
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("PG_ENABLED", "false")
    monkeypatch.setenv("RERANK_ENABLED", "false")
    monkeypatch.setenv("ADJACENT_ENABLED", "false")

    # ⚠️ 关键（2026-09-03，审查整改）：外部付费 API 的凭据必须在测试期清空，
    # 否则测试结果随开发者本机 .env 漂移。已实证：本机 .env 配了 TAVILY_API_KEY
    # 时，`test_tool_result_failure_marked` 与 `test_parallel_tool_calls_sse_events`
    # 会转红——它们断言 web_search 未配置 Key 时返回
    # ToolResult(ok=False, "搜索不可用")，而 build_default_tools 直接读
    # src.agents.tools.TAVILY_API_KEY，配了 Key 就走"可用"分支。
    #
    # 注意 patch 目标是「使用点」而非 src.config：这些名字是被
    # `from src.config import ...` 导入的模块级副本，改 src.config 不影响已导入符号。
    monkeypatch.setattr("src.agents.tools.TAVILY_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")

    # ⚠️ 连接池在测试期强制关闭（2026-09-03 引入连接池后）。原因：db-mock 类
    # 测试（test_intent_v2 / test_query_log / test_memory 等）打桩的是全局
    # psycopg2.connect，而 src.db.pool 的池一旦初始化成功（本机 docker 的 PG
    # 常驻时必然成功）会绕过 mock 直连真实库，测试结果随环境漂移。
    # 关闭后 db_connection() 走一次性直连路径 → 恰好命中这些测试的既有打桩点。
    monkeypatch.setattr("src.db.pool._pool", None, raising=False)
    monkeypatch.setattr("src.db.pool._pool_init_error", "test-env-force-off", raising=False)


@pytest.fixture
def fake_retriever():
    """返回 FakeRetriever 实例（tests/fakes.py）"""
    from tests.fakes import FakeRetriever

    return FakeRetriever()


@pytest.fixture
def fake_tool_llm():
    """返回 FakeToolLLM 实例（tests/fakes.py）"""
    from tests.fakes import FakeToolLLM

    return FakeToolLLM()
