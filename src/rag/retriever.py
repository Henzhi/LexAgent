"""
检索器抽象接口与 pgvector 实现。

v0.6: 移除 FAISS 后端，检索层统一走 pgvector（纯 PG 架构）。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.config import RETRIEVAL_DROP_SUMMARY_CHUNKS, RETRIEVAL_SIM_THRESHOLD

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 检索结果模型
# ---------------------------------------------------------------------------

@dataclass
class RetrievedDoc:
    """单条检索结果"""
    content: str
    score: float
    law_name: str = ""
    chapter: str = ""
    section: str = ""
    article_range: str = ""
    chunk_type: str = ""

    @property
    def citation(self) -> str:
        """生成引用标注，如 '治安管理处罚法 第十条'"""
        parts = [self.law_name]
        if self.article_range:
            parts.append(self.article_range)
        elif self.chapter:
            parts.append(self.chapter)
        return " · ".join(parts)


def doc_to_retrieved(doc, score: float) -> RetrievedDoc:
    """将带 metadata 的文档对象（LangChain Document 等）转换为 RetrievedDoc。

    字段映射：law_name / chapter / section / article_range / chunk_type
    取自 metadata，content 取文档正文。
    """
    meta = getattr(doc, "metadata", {}) or {}
    return RetrievedDoc(
        content=doc.page_content,
        score=round(score, 4),
        law_name=meta.get("law_name", ""),
        chapter=meta.get("chapter", ""),
        section=meta.get("section", ""),
        article_range=meta.get("article_range", ""),
        chunk_type=meta.get("chunk_type", ""),
    )


# ---------------------------------------------------------------------------
# 抽象检索器
# ---------------------------------------------------------------------------

class BaseRetriever(ABC):
    """检索器抽象基类"""

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[RetrievedDoc]:
        """语义检索，返回 top_k 个最相关文档"""
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        """检索器是否已就绪（索引已加载）"""
        ...


# ---------------------------------------------------------------------------
# pgvector v2 检索器（基于 PgvectorStore，纯 PG 默认）
# ---------------------------------------------------------------------------

class PgvectorStoreRetriever(BaseRetriever):
    """基于 PgvectorStore 的检索器（v0.5 企业级）

    使用新的 document_chunks 表 + halfvec + embedding_model 隔离。
    """

    def __init__(
        self,
        store,          # PgvectorStore 实例
        embedder,       # EmbeddingAdapter 或 LawEmbedder
        embedding_model: str | None = None,
    ):
        self._store = store
        self._embedder = embedder
        self._embedding_model = embedding_model or embedder.model

    def search(self, query: str, top_k: int = 5, doc_type: str | None = None) -> list[RetrievedDoc]:
        vec = self._embedder.embed_query(query)
        rows = self._store.search(
            query_vec=vec,
            top_k=top_k,
            embedding_model=self._embedding_model,
            doc_type=doc_type,
            drop_summary=RETRIEVAL_DROP_SUMMARY_CHUNKS,
            sim_threshold=RETRIEVAL_SIM_THRESHOLD,
        )
        return [self._row_to_doc(r) for r in rows]

    def is_ready(self) -> bool:
        return self._store.is_ready()

    @staticmethod
    def _row_to_doc(row: dict) -> RetrievedDoc:
        return RetrievedDoc(
            content=row["content"],
            score=row["score"],
            law_name=row.get("law_name", ""),
            chapter=row.get("chapter", ""),
            section=row.get("section", ""),
            article_range=row.get("article_range", ""),
            chunk_type=row.get("chunk_type", ""),
        )


# ---------------------------------------------------------------------------
# pgvector v1 检索器（旧表 law_chunks，兼容过渡期）
# ---------------------------------------------------------------------------

class PgvectorRetriever(BaseRetriever):
    """基于 PostgreSQL + pgvector 的检索器

    用法:
        retriever = PgvectorRetriever(embedder, connection_string)
        retriever.build_from_documents(docs)  # 首次构建
        results = retriever.search(query, top_k=5)
    """

    def __init__(
        self,
        embedder,       # LawEmbedder 实例
        conn_string: str = "",
        table_name: str = "law_chunks",
    ):
        import psycopg2
        from pgvector.psycopg2 import register_vector

        self._embedder = embedder
        self._table = table_name
        self._conn_string = conn_string
        self._conn = psycopg2.connect(conn_string)
        register_vector(self._conn)
        self._create_table()

    def _ensure_connection(self):
        """检查连接是否存活，断开则自动重连"""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            import psycopg2
            from pgvector.psycopg2 import register_vector
            logger.warning("PG 连接已断开，尝试重连...")
            try:
                self._conn.close()
            except Exception as close_e:
                logger.debug(f"retriever 关闭旧连接失败（可忽略）: {close_e}")
            self._conn = psycopg2.connect(self._conn_string)
            register_vector(self._conn)
            logger.info("PG 重连成功")

    def _create_table(self):
        dim = self._embedder.get_embedding_dim()
        with self._conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding vector({dim}),
                    law_name TEXT DEFAULT '',
                    chapter TEXT DEFAULT '',
                    section TEXT DEFAULT '',
                    article_range TEXT DEFAULT '',
                    chunk_type TEXT DEFAULT ''
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{self._table}_embedding
                ON {self._table} USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """)
        self._conn.commit()

    def build_from_documents(self, documents: list, batch_size: int = 32):
        """从 LangChain Document 列表构建 pgvector 索引"""
        total = len(documents)
        for i in range(0, total, batch_size):
            batch = documents[i:i + batch_size]
            texts = [d.page_content for d in batch]
            embeddings = self._embedder.embed_documents(texts)

            with self._conn.cursor() as cur:
                for doc, emb in zip(batch, embeddings):
                    meta = doc.metadata
                    cur.execute(
                        f"INSERT INTO {self._table} (content,embedding,law_name,chapter,section,article_range,chunk_type) "
                        f"VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            doc.page_content,
                            emb,
                            meta.get("law_name", ""),
                            meta.get("chapter", ""),
                            meta.get("section", ""),
                            meta.get("article_range", ""),
                            meta.get("chunk_type", ""),
                        ),
                    )
            self._conn.commit()
            logger.info(f"pgvector 写入进度: {min(i + batch_size, total)}/{total}")

    def search(self, query: str, top_k: int = 5, doc_type: str | None = None) -> list[RetrievedDoc]:
        """语义检索 — doc_type 在旧 pgvector 模式下忽略（兼容接口）"""
        self._ensure_connection()
        vec = self._embedder.embed_query(query)
        where = "WHERE chunk_type <> 'chapter_summary' " if RETRIEVAL_DROP_SUMMARY_CHUNKS else ""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT content,law_name,chapter,section,article_range,chunk_type,"
                f"1 - (embedding <=> %s::vector) AS score "
                f"FROM {self._table} {where}"
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, vec, top_k),
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            content, law, ch, sec, article, ctype, score = row
            results.append(RetrievedDoc(
                content=content,
                score=round(float(score), 4),
                law_name=law or "",
                chapter=ch or "",
                section=sec or "",
                article_range=article or "",
                chunk_type=ctype or "",
            ))
        return results

    def search_by_law(self, query: str, law_name: str, top_k: int = 5) -> list[RetrievedDoc]:
        self._ensure_connection()
        vec = self._embedder.embed_query(query)
        where = "AND chunk_type <> 'chapter_summary' " if RETRIEVAL_DROP_SUMMARY_CHUNKS else ""
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT content,law_name,chapter,section,article_range,chunk_type,"
                f"1 - (embedding <=> %s::vector) AS score "
                f"FROM {self._table} WHERE law_name = %s {where}"
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, law_name, vec, top_k),
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            content, law, ch, sec, article, ctype, score = row
            results.append(RetrievedDoc(
                content=content,
                score=round(float(score), 4),
                law_name=law or "",
                chapter=ch or "",
                section=sec or "",
                article_range=article or "",
                chunk_type=ctype or "",
            ))
        return results

    def is_ready(self) -> bool:
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self._table}")
                return cur.fetchone()[0] > 0
        except Exception:
            return False

    def close(self):
        self._conn.close()
