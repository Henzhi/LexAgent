"""HTTP 层端到端：用 TestClient 直接挂载 routes.router 调用 /api/rewrite。

不依赖 PostgreSQL / 主应用 lifespan（那部分需要 PG 才能启动），
只验证路由绑定、Pydantic 解析与改写链路（LLM 以 mock 代替，避免依赖外部服务）。
"""
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import router


class _FakeLLM:
    def __init__(self, out):
        self.out = out

    def chat(self, prompt, history=None, system_prompt=None):
        return self.out


_app = FastAPI()
_app.include_router(router, prefix="/api")  # 镜像 main.py 的挂载方式
_client = TestClient(_app)


def test_rewrite_endpoint_colloquial():
    fake = _FakeLLM("用人单位拖欠劳动报酬的法律责任与维权途径")
    with patch("src.api.routes.get_llm", return_value=fake):
        r = _client.post("/api/rewrite", json={"query": "老板一直拖着工资不给，我该怎么办"})
    assert r.status_code == 200
    data = r.json()
    assert data["changed"] is True
    assert "劳动报酬" in data["proposed_query"]


def test_rewrite_endpoint_precise_keeps_meaning():
    fake = _FakeLLM("刑法第232条的定罪量刑规定")
    with patch("src.api.routes.get_llm", return_value=fake):
        r = _client.post("/api/rewrite", json={"query": "刑法第232条是什么"})
    assert r.status_code == 200
    data = r.json()
    assert "刑法第232条" in data["proposed_query"]


def test_rewrite_endpoint_empty_query_rejected():
    r = _client.post("/api/rewrite", json={"query": ""})
    assert r.status_code == 422  # Pydantic 校验拒绝空串
