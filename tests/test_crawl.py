"""爬取功能单元测试（离线，不访问真实站点）。

覆盖:
  - 类型映射与未支持类型校验
  - 文件落地格式（首行为标题）与 manifest 增量记录往返
  - FastAPI 路由: /api/crawl/types 与 /api/crawl 任务提交 + 状态轮询
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.knowledge.crawler import NpcLawCrawler, TYPE_MAP
from src.knowledge.crawler.npc_crawler import CrawlResult


# ---------------------------------------------------------------------------
# 1. 类型映射 / 校验
# ---------------------------------------------------------------------------

def test_type_map_keys():
    # flk 顶级分类规范值（v0.6 对齐国家法律法规数据库分类）
    assert set(TYPE_MAP.keys()) >= {
        "constitution", "law", "regulation", "supervision",
        "judicial_interpretation", "local_regulation"
    }
    # 每个类型都有 (flfg_code_ids, subdir) 二元组
    for codes, subdir in TYPE_MAP.values():
        assert isinstance(codes, (list, tuple)) and codes
        assert all(isinstance(c, int) for c in codes)


def test_doc_type_normalize():
    """历史旧值归一到规范 doc_type"""
    from src.knowledge.doc_types import normalize_doc_type
    assert normalize_doc_type("judicial") == "judicial_interpretation"
    assert normalize_doc_type("interpretation") == "judicial_interpretation"
    assert normalize_doc_type("local") == "local_regulation"
    assert normalize_doc_type("law") == "law"          # 已是规范值
    assert normalize_doc_type("  REGULATION ") == "regulation"  # 大小写/空白容忍


def test_status_from_sxx():
    """flk 效力状态码映射：1废止/2已修改/3有效/4未生效"""
    from src.knowledge.doc_types import status_from_sxx
    assert status_from_sxx("1") == "repealed"
    assert status_from_sxx("2") == "revised"
    assert status_from_sxx("3") == "active"
    assert status_from_sxx("4") == "pending"
    assert status_from_sxx(None) == "active"     # 未知/缺失兜底为有效
    assert status_from_sxx(1) == "repealed"      # 数字形式兼容


def test_status_label():
    from src.knowledge.doc_types import status_label
    assert status_label("active") == "现行有效"
    assert status_label("repealed") == "已废止"
    assert status_label("pending") == "尚未生效"


def test_doc_type_from_flxz():
    """flk 法律形式 flxz 自动分类映射"""
    from src.knowledge.doc_types import doc_type_from_flxz
    assert doc_type_from_flxz("宪法") == "constitution"
    assert doc_type_from_flxz("法律") == "law"
    assert doc_type_from_flxz("行政法规") == "regulation"
    assert doc_type_from_flxz("监察法规") == "supervision"
    assert doc_type_from_flxz("地方性法规") == "local_regulation"
    assert doc_type_from_flxz("司法解释") == "judicial_interpretation"
    assert doc_type_from_flxz("自治条例和单行条例") == "local_regulation"
    assert doc_type_from_flxz("经济特区法规") == "local_regulation"
    assert doc_type_from_flxz("未知类型") is None
    assert doc_type_from_flxz(None) is None
    assert doc_type_from_flxz("最高人民法院 司法解释") == "judicial_interpretation"  # 包含匹配


def test_crawl_auto_accepted():
    """auto 自动分类模式被 crawl() 接受且不被判为不支持"""
    from src.knowledge.crawler.npc_crawler import UNSUPPORTED_TYPES
    assert "auto" not in UNSUPPORTED_TYPES


def test_crawl_auto_skip_existing_by_status():
    """自动分类 + 跳过已入库：同标题同状态跳过，不同效力版本各自入库"""
    import tempfile
    from unittest import mock

    from src.knowledge.crawler.npc_crawler import NpcLawCrawler

    tmp = tempfile.mkdtemp()
    crawler = NpcLawCrawler(law_data_dir=tmp, sleep=0)

    # fake pg store：记录按 (title, status) 的去重查询，并记录写入调用
    class FakeStore:
        def __init__(self):
            self.known = {}  # (title, status) -> doc_id
            self.writes = []

        def get_document_id_by_title(self, title, status=None):
            return self.known.get((title, status or "active"))

        def ensure_document(self, doc_type, title, source="", effective_date=None, status="active"):
            key = (title, status)
            if key not in self.known:
                self.known[key] = f"id-{len(self.known)}"
            return self.known[key]

    class FakePipeline:
        def __init__(self, store):
            self.store = store

        def ingest_text(self, title, text, doc_type="law", source="", effective_date=None, force=False, status="active"):
            self.store.writes.append((title, status, doc_type))
            self.store.known[(title, status)] = "doc-exists"
            return 5

    store = FakeStore()
    crawler._pg_store = store
    crawler._pg_pipeline = FakePipeline(store)

    # 列表命中 3 条：同标题不同效力 + 一个完全不同标题
    items = [
        {"bbbs": "b1", "title": "刑法", "flxz": "法律", "sxx": "3"},   # active
        {"bbbs": "b2", "title": "刑法", "flxz": "法律", "sxx": "1"},   # repealed（同标题不同状态）
        {"bbbs": "b3", "title": "某司法解释", "flxz": "司法解释", "sxx": "3"},
    ]
    crawler._fetch_list = mock.Mock(return_value=items)
    crawler._fetch_document = mock.Mock(return_value="某法律条文正文内容足够长满足长度要求" * 3)

    # 预置：刑法 active 已入库
    store.known[("刑法", "active")] = "doc-penal-active"

    result = crawler.crawl(doc_type="auto", limit=10, store="pg")

    # 刑法 active 已存在 -> skipped；刑法 repealed 新 -> added；司法解释 -> added
    assert result.skipped == 1, f"expected skip 1, got {result.skipped}"
    assert result.added == 2, f"expected add 2, got {result.added}"
    writes = store.writes
    assert ("刑法", "repealed", "law") in writes
    assert ("某司法解释", "active", "judicial_interpretation") in writes
    assert not any(w[0] == "刑法" and w[1] == "active" for w in writes)


def test_unsupported_case_raises():
    crawler = NpcLawCrawler(law_data_dir=Path("/tmp/__noop__"))
    with pytest.raises(ValueError):
        crawler.crawl(doc_type="case")


# ---------------------------------------------------------------------------
# 2. 落地格式 + manifest 往返（临时目录，不联网）
# ---------------------------------------------------------------------------

def test_save_format_and_manifest_roundtrip(tmp_path: Path):
    crawler = NpcLawCrawler(law_data_dir=tmp_path)

    out_dir = tmp_path / "laws"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 写一条
    rel = crawler._save(out_dir, "id001", "中华人民共和国刑法", "第一条 为了惩罚犯罪……", "2024-01-01")
    file_path = tmp_path / rel
    assert file_path.exists()
    text = file_path.read_text(encoding="utf-8")
    assert text.startswith("中华人民共和国刑法\n\n"), "首行应为法律标题"
    assert "第一条" in text

    # manifest 写入后可读回一致
    crawler._save_manifest(out_dir, {"id001": {"id": "id001", "title": "中华人民共和国刑法"}})
    loaded = crawler._load_manifest(out_dir)
    assert loaded["id001"]["title"] == "中华人民共和国刑法"

    # 文件名非法字符被清洗（如含 / 或 : 的标题）
    rel2 = crawler._save(out_dir, "id002", '测试/法:规"', "内容", "")
    # 只校验文件名部分（返回值为相对路径，目录分隔符在 Windows/Linux 上不同）
    assert "/" not in Path(rel2).name and ":" not in Path(rel2).name


# ---------------------------------------------------------------------------
# 3. API 路由（最小 app，避免触发主 lifespan 加载引擎）
# ---------------------------------------------------------------------------

def _make_client() -> TestClient:
    app = FastAPI()
    from src.api.routes import router as api_router
    from src.api.auth import require_registered_user
    # 管理接口已要求登录（审计修复），测试中覆写依赖以离线通过
    app.dependency_overrides[require_registered_user] = lambda: "test-user"
    app.include_router(api_router, prefix="/api")
    return TestClient(app)


def test_crawl_types_endpoint():
    client = _make_client()
    resp = client.get("/api/crawl/types")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "npc"
    assert "law" in data["types"]
    # unsupported 是字符串列表，校验其中条目提及 case
    assert any("case" in s for s in data["unsupported"])


def test_crawl_submit_and_status(monkeypatch):
    """用假爬虫替换真实爬虫，验证任务提交→状态轮询→结果流程（离线）。"""

    class FakeCrawler:
        def crawl(self, **kwargs):
            # 校验参数确实传到了爬虫
            assert kwargs["doc_type"] == "law"
            return CrawlResult(total=1, added=1, updated=0, skipped=0, failed=0,
                               files=["laws/test.txt"], errors=[])

    monkeypatch.setattr("src.knowledge.crawler.NpcLawCrawler", FakeCrawler)

    client = _make_client()
    resp = client.post("/api/crawl", json={"doc_type": "law", "limit": 1})
    assert resp.status_code == 200
    body = resp.json()
    task_id = body["task_id"]
    assert body["status"] == "pending"

    # 轮询直到完成
    status = None
    for _ in range(50):
        status = client.get(f"/api/crawl/status/{task_id}").json()
        if status["finished"]:
            break
        time.sleep(0.1)

    assert status is not None and status["finished"], "爬取任务应在超时前完成"
    assert status["status"] == "done"
    assert status["result"]["added"] == 1
    assert status["progress"]["total"] == 1


def test_crawl_status_404():
    client = _make_client()
    resp = client.get("/api/crawl/status/nonexistent")
    assert resp.status_code == 404
