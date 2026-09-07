"""D-0907-1 守护测试：检索路径必须只召回 active 文档的 chunk。

背景：知识库一度全是 `status='active'`，旧法版本与重复入库副本一起被检索
（刑法两版 630/631 chunks 同为 active，问民企腐败会召回不含修正案十二的旧版）。
治理方式是给失效/重复文档打 status 标记，因此**每条检索路径都必须带 status 过滤**——
漏一条，标记就形同虚设。

⚠️ 实现纪律（2026-09-07 CI 实证）：本文件最初用「monkeypatch 模块级 db_connection +
自建 _FakeCursor 捕获 SQL」的执行式断言，CI 全量跑时污染了 test_usage_api 的
lifespan（ensure_tables 拿到 fake cursor 报 AttributeError，13 例 ERROR）——
与项目 conftest / test_usage_store / test_db_pool 的同名 _FakeCursor 生态互相
干扰，patch 生命周期无法保证与其他 TestClient 型测试隔离。
已改为**源码级断言**（先例：test_f15_usage_ddl.py 文件级守卫）：零 patch、零泄漏。

测试策略：读取三个检索模块源码，断言每条检索 SQL 构造处都带 `d.status = 'active'`
谓词。新增检索路径漏过滤会直接红。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PGVECTOR_STORE = ROOT / "src" / "knowledge" / "pgvector_store.py"
ARTICLE_ROUTER = ROOT / "src" / "rag" / "article_router.py"
LAW_CENTROIDS = ROOT / "src" / "rag" / "law_centroids.py"

# 检索 SQL 的 status 谓词（各处源码中的统一写法）
ACTIVE_PREDICATE = "d.status = 'active'"


def _func_body(path: Path, func_name: str) -> str:
    """提取函数体源码（从 def 行到下一个同缩进 def/class/装饰器）。

    足够守护「SQL 模板里有没有谓词」这类文本级断言，不追求 AST 精度。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    indent = 0
    for i, line in enumerate(lines):
        if line.strip().startswith(f"def {func_name}("):
            start = i
            indent = len(line) - len(line.lstrip())
            break
    assert start is not None, f"{path.name} 中找不到函数 {func_name}"
    body = [lines[start]]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        cur_indent = len(line) - len(line.lstrip())
        if stripped and not stripped.startswith("#") and cur_indent <= indent:
            if stripped.startswith(("def ", "async def ", "class ", "@")):
                break
        body.append(line)
    return "\n".join(body)


class TestSearchPathsFilterActive:
    def test_pgvector_vector_search_filters_active(self):
        """主向量检索（HNSW）：失效/重复文档的 chunk 不得进入召回。"""
        body = _func_body(PGVECTOR_STORE, "search")
        assert ACTIVE_PREDICATE in body, (
            "PgvectorStore.search 丢失了 d.status = 'active' 谓词——superseded/duplicate 文档将重新参与检索（D-0907-1）"
        )

    def test_bm25_source_filters_active(self):
        """BM25 索引构建源：重复副本若进索引会挤占词频与召回位次。"""
        body = _func_body(PGVECTOR_STORE, "fetch_all_active_chunks")
        assert ACTIVE_PREDICATE in body, "fetch_all_active_chunks 丢失 status 谓词（D-0907-1）"

    def test_law_centroids_filters_active(self):
        """法名质心：失效版本混入会稀释质心，让法名推断加权失真。"""
        body = _func_body(LAW_CENTROIDS, "_load_rows")
        assert ACTIVE_PREDICATE in body, "law_centroids._load_rows 丢失 status 谓词（D-0907-1）"

    def test_article_router_filters_active(self):
        """条文路由（法名+条号精确查库）：同样不得召回失效版本。"""
        body = _func_body(ARTICLE_ROUTER, "_query_db")
        assert ACTIVE_PREDICATE in body, "article_router._query_db 丢失 status 谓词（D-0907-1）"


class TestIngestTitleNormalization:
    def test_ingest_normalizes_title_before_dedupe(self):
        """D-0907-3：入库标题必须归一，否则尾空格会让同一部法被重复入库。"""
        from src.knowledge.ingestion.pipeline import normalize_document_title

        assert normalize_document_title(" 中华人民共和国公司法(2023修订) ") == ("中华人民共和国公司法(2023修订)")
        assert normalize_document_title("某法  名称\n\t带空白 ") == "某法 名称 带空白"
        assert normalize_document_title(None) == ""
