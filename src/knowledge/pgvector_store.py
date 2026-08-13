"""
PostgreSQL + pgvector 知识库存储层 (v0.5)

企业级升级:
  - documents 主表 + document_chunks 块表，支持版本管理和状态标记
  - halfvec 半精度向量，存储减半、检索提速 ~30%
  - embedding_model 列隔离不同模型，切换模型无需全量重建
  - HNSW 索引，10万+ 向量仍保持 <10ms 延迟
  - 增量索引：单条 INSERT 即可生效，无需重建

用法:
    store = PgvectorStore(conn_string)
    store.ensure_tables()
    store.insert_chunks(chunks, embedding_model="bge-m3")
    results = store.search(query_vec, top_k=5)
"""
from __future__ import annotations

import logging
import threading
from functools import wraps
from typing import List

logger = logging.getLogger(__name__)


def _locked(method):
    """串行化对共享 PG 连接的访问。

    psycopg2 连接非线程安全。流式桥接改造后，多个请求可能并发调用本
    store（各占一个线程池 worker），必须用锁保护同一连接，否则会出现
    cursor 冲突 / 连接被并发使用等错误。
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_lock", None)
        if lock is None:
            # 防御：兼容绕过 __init__ 的构造方式（如测试 mock）
            lock = threading.Lock()
            self._lock = lock
        with lock:
            return method(self, *args, **kwargs)
    return wrapper


class PgvectorStore:
    """pgvector 知识库存储

    封装所有 PG + pgvector 操作，提供:
      - 表结构初始化
      - 文档块批量写入
      - 向量检索（余弦相似度）
      - 文档/块管理（状态切换、删除）
    """

    def __init__(self, conn_string: str):
        import psycopg2
        from pgvector.psycopg2 import register_vector

        self._conn_string = conn_string
        self._lock = threading.Lock()
        self._conn = psycopg2.connect(conn_string)
        register_vector(self._conn)

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _ensure_connection(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            import psycopg2
            from pgvector.psycopg2 import register_vector
            logger.warning("PG 连接断开，重连中...")
            try:
                self._conn.close()
            except Exception as close_e:
                logger.debug(f"PG 关闭旧连接失败（可忽略）: {close_e}")
            self._conn = psycopg2.connect(self._conn_string)
            register_vector(self._conn)

    @_locked
    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------
    # 表初始化
    # ------------------------------------------------------------------

    @_locked
    def ensure_tables(self):
        """创建知识库相关表（幂等，已有表不重建）"""
        self._ensure_connection()
        # 表结构由 docker/init.sql 定义，这里仅做存在性检查
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='document_chunks'")
            if cur.fetchone() is None:
                raise RuntimeError(
                    "document_chunks 表不存在。请先运行 docker compose up -d 初始化数据库。"
                )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 文档管理
    # ------------------------------------------------------------------

    @_locked
    def ensure_document(
        self,
        doc_type: str,
        title: str,
        source: str = "",
        effective_date: str | None = None,
        status: str = "active",
    ) -> str:
        """获取或创建文档记录，返回 doc_id

        Args:
            doc_type: 文档类型（规范 doc_type）
            title: 标题（用于去重：同标题 + 同 status 视为同一条）
            source: 来源
            effective_date: 生效日期
            status: 效力状态（active/repealed/revised/pending）。
                    同一法律的不同效力版本以 (title, status) 区分。
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM documents WHERE title = %s AND status = %s",
                (title, status),
            )
            row = cur.fetchone()
            if row:
                return str(row[0])

            cur.execute(
                "INSERT INTO documents (doc_type, title, source, effective_date, status) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (doc_type, title, source, effective_date, status),
            )
            doc_id = str(cur.fetchone()[0])
        self._conn.commit()
        logger.info(f"新建文档: [{doc_type}] {title} (id={doc_id[:8]}..., status={status})")
        return doc_id

    def get_document_id_by_title(self, title: str, status: str | None = None) -> str | None:
        """按标题查询已存在文档的 id，不存在返回 None。

        用于爬虫增量去重：命中说明该法律已入库，可跳过。
        status 指定时精确匹配该效力状态；None 时优先返回 active，
        否则返回任意一条（用于全状态入库时的去重判断）。
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT id FROM documents WHERE title = %s AND status = %s",
                    (title, status),
                )
            else:
                cur.execute(
                    "SELECT id FROM documents WHERE title = %s ORDER BY "
                    "(status = 'active') DESC LIMIT 1",
                    (title,),
                )
            row = cur.fetchone()
        return str(row[0]) if row else None

    # ------------------------------------------------------------------
    # 块写入
    # ------------------------------------------------------------------

    def insert_chunks(
        self,
        chunks: list[dict],
        embedding_model: str,
        batch_size: int = 32,
    ) -> int:
        """批量写入文档块

        Args:
            chunks: [{"doc_id", "chunk_type", "content", "embedding", "metadata"}, ...]
            embedding_model: 嵌入模型标识，如 "bge-m3"
            batch_size: 每批提交数

        Returns:
            写入的块数量
        """
        import json as _json
        self._ensure_connection()
        total = 0
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            with self._conn.cursor() as cur:
                for c in batch:
                    embedding = c["embedding"]
                    meta = c.get("metadata", {})
                    # dict → JSON 字符串，PG 自动转 JSONB
                    if isinstance(meta, dict):
                        meta = _json.dumps(meta, ensure_ascii=False)
                    cur.execute(
                        "INSERT INTO document_chunks "
                        "(doc_id, chunk_type, content, embedding_model, embedding, metadata) "
                        "VALUES (%s, %s, %s, %s, %s::halfvec, %s)",
                        (
                            c["doc_id"],
                            c.get("chunk_type", "article"),
                            c["content"],
                            embedding_model,
                            embedding,
                            meta,
                        ),
                    )
            self._conn.commit()
            total += len(batch)
        logger.info(f"pgvector 写入完成: {total} chunks, model={embedding_model}")
        return total

    _SORT_COLUMNS = {"created_at", "updated_at", "title", "doc_type"}

    def list_documents(
        self,
        doc_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """列出文档列表（分页 + 排序 + 关键词搜索）

        Args:
            doc_type: 按类型过滤，None=全部
            status: 效力状态过滤，None=全部；也可传逗号分隔的多个（如 "active,repealed"）
            q: 关键词，同时匹配标题与文档块内容（大小写不敏感），None/空=不过滤
            sort: 排序字段（白名单: created_at/updated_at/title/doc_type），非法值回退 created_at
            order: "asc" / "desc"
            limit: 每页条数（建议 ≤200）
            offset: 跳过条数（配合前端无限滚动分页）

        Returns:
            (docs, total)
            docs: [{id, title, doc_type, source, effective_date, status,
                    created_at, updated_at, chunks}, ...]
        """
        self._ensure_connection()
        statuses = [s.strip() for s in (status or "").split(",") if s.strip()] if status else None

        where: list[str] = []
        params: list = []
        if statuses:
            placeholders = ", ".join(["%s"] * len(statuses))
            where.append(f"d.status IN ({placeholders})")
            params.extend(statuses)
        if doc_type:
            where.append("d.doc_type = %s")
            params.append(doc_type)
        if q and q.strip():
            like = f"%{q.strip()}%"
            where.append(
                "(d.title ILIKE %s OR EXISTS (SELECT 1 FROM document_chunks dc "
                "WHERE dc.doc_id = d.id AND dc.content ILIKE %s))"
            )
            params.extend([like, like])
        where_sql = " AND ".join(where) if where else "TRUE"

        col = sort if sort in self._SORT_COLUMNS else "created_at"
        direction = "ASC" if (order or "").lower() == "asc" else "DESC"

        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(DISTINCT d.id) FROM documents d WHERE {where_sql}", params)
            total = int(cur.fetchone()[0])

            sql = f"""SELECT d.id, d.title, d.doc_type, d.source, d.effective_date,
                             d.status, d.created_at, d.updated_at, COUNT(dc.id) AS chunks
                      FROM documents d
                      LEFT JOIN document_chunks dc ON d.id = dc.doc_id
                      WHERE {where_sql}
                      GROUP BY d.id
                      ORDER BY d.{col} {direction}
                      LIMIT %s OFFSET %s"""
            cur.execute(sql, params + [int(limit), int(offset)])
            rows = cur.fetchall()

        docs = [
            {
                "id": str(row[0]),
                "title": row[1],
                "doc_type": row[2],
                "source": row[3] or "",
                "effective_date": str(row[4]) if row[4] else "",
                "status": row[5],
                "created_at": str(row[6]) if row[6] else "",
                "updated_at": str(row[7]) if row[7] else "",
                "chunks": int(row[8]),
            }
            for row in rows
        ]
        return docs, total

    def delete_document(self, doc_id: str) -> bool:
        """删除文档及其所有块（级联删除）

        Args:
            doc_id: 文档 UUID

        Returns:
            是否成功删除
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            deleted = cur.rowcount
        self._conn.commit()
        if deleted:
            logger.info(f"已删除文档: id={doc_id[:8]}... (含所有块)")
        return deleted > 0

    @_locked
    def get_document_chunks(
        self, doc_id: str, limit: int | None = None, offset: int = 0
    ) -> list[dict]:
        """获取文档的文本块（不含向量），支持分页

        Args:
            doc_id: 文档 UUID
            limit: 每页条数，None=不分页返回全部
            offset: 跳过条数（配合 limit 使用）

        Returns:
            [{id, chunk_type, content, embedding_model, metadata, created_at}, ...]
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            sql = """SELECT id, chunk_type, content, embedding_model, metadata, created_at
                     FROM document_chunks
                     WHERE doc_id = %s
                     ORDER BY created_at"""
            params: list = [doc_id]
            if limit is not None:
                sql += " LIMIT %s OFFSET %s"
                params += [limit, offset]
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        results = []
        for row in rows:
            meta = row[4] or {}
            if isinstance(meta, str):
                import json as _json
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            results.append({
                "id": str(row[0]),
                "chunk_type": row[1],
                "content": row[2],
                "embedding_model": row[3],
                "metadata": meta,
                "created_at": str(row[5]) if row[5] else "",
            })
        return results

    def count_document_chunks(self, doc_id: str) -> int:
        """统计文档的块数量（用于分页）"""
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM document_chunks WHERE doc_id = %s", (doc_id,))
            row = cur.fetchone()
        return int(row[0]) if row and row[0] else 0

    def get_chunk_count(self) -> int:
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM document_chunks")
            return cur.fetchone()[0]

    @property
    def doc_count(self) -> int:
        """文档总数（实时查库，供健康检查等展示）。

        v0.6 纯 PG：FAISS 时代的 doc_count 是内存缓存属性，迁移后未补齐，
        导致健康检查恒为 0。这里改为实时查询 documents 表。
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM documents")
            return cur.fetchone()[0] or 0

    # ------------------------------------------------------------------
    # 向量检索
    # ------------------------------------------------------------------

    @_locked
    def search(
        self,
        query_vec: List[float],
        top_k: int = 5,
        embedding_model: str | None = None,
        doc_type: str | None = None,
        drop_summary: bool = True,
        sim_threshold: float = 0.0,
    ) -> list[dict]:
        """余弦相似度检索

        Args:
            query_vec: 查询向量
            top_k: 返回条数
            embedding_model: 仅检索指定模型的向量（None=不过滤）
            doc_type: 仅检索指定类型的文档（None=不过滤）
            drop_summary: 是否丢弃 chapter_summary 噪声
            sim_threshold: 最低相似度阈值（0=关闭）

        Returns:
            [{"content", "score", "law_name", "chapter", "article_range", ...}, ...]
        """
        self._ensure_connection()

        conditions = []
        # params 顺序必须匹配 SQL: SELECT %s → WHERE %s ... → ORDER BY %s → LIMIT %s
        params = [query_vec]  # SELECT 子句中的向量

        # embedding_model 过滤
        if embedding_model:
            conditions.append("dc.embedding_model = %s")
            params.append(embedding_model)
        # doc_type 过滤
        if doc_type:
            conditions.append("d.doc_type = %s")
            params.append(doc_type)
        # 噪声过滤
        if drop_summary:
            conditions.append("dc.chunk_type <> 'chapter_summary'")
        # 仅 active 文档
        conditions.append("d.status = 'active'")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        # ORDER BY 向量 + LIMIT
        params.append(query_vec)
        params.append(top_k)

        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT dc.content, dc.metadata, dc.embedding_model, "
                f"1 - (dc.embedding <=> %s::halfvec) AS score "
                f"FROM document_chunks dc "
                f"JOIN documents d ON dc.doc_id = d.id "
                f"{where} "
                f"ORDER BY dc.embedding <=> %s::halfvec "
                f"LIMIT %s",
                params,
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            content, metadata, model, score = row
            meta = metadata or {}
            results.append({
                "content": content,
                "score": round(float(score), 4),
                "law_name": meta.get("law_name", ""),
                "chapter": meta.get("chapter", ""),
                "section": meta.get("section", ""),
                "article_range": meta.get("article_range", ""),
                "chunk_type": meta.get("chunk_type", ""),
                "embedding_model": model,
            })

        # 相似度阈值过滤
        if sim_threshold > 0 and results:
            filtered = [r for r in results if r["score"] >= sim_threshold]
            if not filtered:
                logger.warning(
                    f"pgvector 阈值 {sim_threshold} 过滤后无候选，回退保留 {len(results)} 条"
                )
                return results[:top_k]
            return filtered[:top_k]

        return results

    def is_ready(self) -> bool:
        try:
            return self.get_chunk_count() > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 索引管理
    # ------------------------------------------------------------------

    @_locked
    def fetch_all_active_chunks(self) -> list[tuple[str, dict]]:
        """返回全部 active 文档块 (content, metadata)。

        供 BM25 索引构建使用。必须通过本方法（已加锁）读取，
        避免外部直接访问共享连接造成线程竞争。
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT dc.content, dc.metadata "
                "FROM document_chunks dc "
                "JOIN documents d ON dc.doc_id = d.id "
                "WHERE d.status = 'active'"
            )
            return cur.fetchall()

    @_locked
    def reindex(self):
        """重建 HNSW 索引（大量写入后建议执行）"""
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute("REINDEX INDEX idx_chunks_embedding")
        self._conn.commit()
        logger.info("HNSW 索引重建完成")
