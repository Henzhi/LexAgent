"""
文档解析管道主流程。

将上传的 PDF/DOCX 文件解析 → 清洗 → 分块 → 向量化 → 写入 pgvector。
支持异步任务状态追踪。
"""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from src.knowledge.ingestion.pdf_parser import PDFParser
from src.knowledge.ingestion.docx_parser import DocxParser
from src.knowledge.ingestion.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)

# 匹配条号，如「第二百三十二条」「第232条」
_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千零两0-9]+条")

# 全文模式：这些 doc_type 整篇作为一个 chunk（直接全文召回，不切分）。
# 默认空 = 全部按各自的差异化切分规则；需要全文召回时通过环境变量
# FULLTEXT_DOC_TYPES="interpretation,case" 开启，或用 set_fulltext_doc_types() 设置
FULLTEXT_DOC_TYPES: set[str] = set()


def set_fulltext_doc_types(doc_types: list[str]) -> None:
    """开启指定 doc_type 的全文模式（整篇一个 chunk，直接全文召回）。"""
    global FULLTEXT_DOC_TYPES
    FULLTEXT_DOC_TYPES = set(doc_types)


def _extract_article_range(content: str) -> str:
    """从条文内容中提取首个条号，用于检索结果的「引用条文」展示。"""
    m = _ARTICLE_RE.search(content or "")
    return m.group(0) if m else ""

# 支持的文件类型
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
MAX_FILE_SIZE_MB = 50

# 任务状态
class TaskStatus:
    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    DONE = "done"
    FAILED = "failed"


class IngestionPipeline:
    """文档解析管道

    用法:
        pipeline = IngestionPipeline(store, embedder)
        task_id = pipeline.submit("path/to/law.pdf", doc_type="law")
        status = pipeline.get_status(task_id)
    """

    def __init__(self, store, embedder):
        self._store = store          # PgvectorStore
        self._embedder = embedder    # EmbeddingAdapter
        self._pdf_parser = PDFParser()
        self._docx_parser = DocxParser()
        self._cleaner = TextCleaner()
        self._tasks: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------

    def submit(
        self,
        file_path: str,
        doc_type: str = "law",
        source: str = "",
        effective_date: str | None = None,
        status: str = "active",
    ) -> str:
        """提交解析任务，返回 task_id

        Args:
            status: 法律效力状态（active/repealed/revised/pending），
                    手工上传时可标注；默认 active（现行有效）
        """
        task_id = str(uuid.uuid4())
        self._tasks[task_id] = {
            "status": TaskStatus.PENDING,
            "file_path": file_path,
            "doc_type": doc_type,
            "source": source,
            "effective_date": effective_date,
            "doc_status": status,  # 法律效力状态，区别于任务状态 task["status"]
            "progress": 0,
            "error": None,
        }
        logger.info(f"解析任务已提交: task_id={task_id[:8]}..., file={file_path}, status={status}")
        return task_id

    def get_status(self, task_id: str) -> dict | None:
        """查询任务状态"""
        return self._tasks.get(task_id)

    def run(self, task_id: str) -> int:
        """同步执行解析任务，返回写入的块数量"""
        task = self._tasks.get(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        try:
            file_path = task["file_path"]
            file_name = Path(file_path).name
            ext = Path(file_path).suffix.lower()

            # 1. 解析
            task["status"] = TaskStatus.PARSING
            raw_text = self._parse_file(file_path, ext)

            # 2. 清洗
            task["status"] = TaskStatus.CHUNKING
            cleaned = self._cleaner.clean(raw_text)
            task["progress"] = 30

            if not cleaned or len(cleaned) < 20:
                raise ValueError(f"解析后文本过短（{len(cleaned)}字符），可能为空白或扫描件")

            # 3. 创建文档记录（透传效力状态）
            doc_id = self._store.ensure_document(
                doc_type=task["doc_type"],
                title=task.get("title") or file_name.replace(ext, ""),
                source=task.get("source", ""),
                effective_date=task.get("effective_date"),
                status=task.get("doc_status", "active"),
            )

            # 4. 分块 — 以段落为边界，500 字一段
            task["status"] = TaskStatus.EMBEDDING
            chunks = self._split_paragraphs(
                cleaned, doc_id, doc_type=task["doc_type"],
                title=task.get("title") or file_name.replace(ext, ""),
            )
            task["progress"] = 60

            # 5. 向量化 + 写入
            task["status"] = TaskStatus.INDEXING
            for i in range(0, len(chunks), self._embedder.batch_size):
                batch = chunks[i:i + self._embedder.batch_size]
                texts = [c["content"] for c in batch]
                embeddings = self._embedder.embed_documents(texts)
                for c, emb in zip(batch, embeddings):
                    c["embedding"] = emb
                self._store.insert_chunks(batch, embedding_model=self._embedder.model)
                task["progress"] = 60 + int(40 * (i + len(batch)) / len(chunks))

            task["status"] = TaskStatus.DONE
            task["progress"] = 100
            # 注意：HNSW 索引支持增量插入，无需每文档 REINDEX（全量重建会锁表）。
            # 批量导入结束后如需整理索引，由调用方显式执行一次 store.reindex()。
            self._rebuild_article_map(chunks)
            logger.info(f"解析完成: {file_name} → {len(chunks)} 块")
            return len(chunks)

        except Exception as e:
            task["status"] = TaskStatus.FAILED
            task["error"] = str(e)
            logger.error(f"解析失败: {task['file_path']} — {e}")
            raise

    def _rebuild_article_map(self, chunks: list[dict]) -> None:
        """重建相邻条文映射 article_map.json（供 AdjacentExpander 使用）。

        原由 scripts/build_index.py（FAISS 时代）生成；v0.6 纯 PG 后，
        每次入库后从本批 chunks 的 metadata 增量更新：
            {law_name: {article_number_int: {content, article_range, chapter, section}}}
        """
        try:
            from pathlib import Path
            import json as _json

            map_path = Path(__file__).resolve().parents[2] / "data" / "vector_store" / "article_map.json"

            existing: dict = {}
            if map_path.exists():
                with open(map_path, encoding="utf-8") as f:
                    existing = _json.load(f)

            cn_to_int = {
                '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
                '百': 100, '千': 1000,
            }

            def _cn2int(cn: str) -> int:
                result = 0
                unit = 1
                i = len(cn) - 1
                while i >= 0:
                    val = cn_to_int.get(cn[i], 0)
                    if val >= 10:
                        unit = val
                        if i == 0:
                            result += unit
                        i -= 1
                        continue
                    result += val * unit
                    unit = 1
                    i -= 1
                return result

            for c in chunks:
                meta = c.get("metadata", {}) or {}
                law_name = meta.get("law_name", "")
                article_range = meta.get("article_range", "")
                if not law_name or not article_range:
                    continue
                import re as _re
                m = _re.search(r'第([一二三四五六七八九十百千零两]+)条', article_range)
                if not m:
                    continue
                num = _cn2int(m.group(1))
                existing.setdefault(law_name, {})[str(num)] = {
                    "content": c.get("content", ""),
                    "article_range": article_range,
                    "chapter": meta.get("chapter", ""),
                    "section": meta.get("section", ""),
                }

            map_path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：先写临时文件再 rename，避免并发进程读到半写状态
            # （多进程并发入库时 JSON 会损坏，表现为 "Extra data" JSONDecodeError）
            import os as _os
            tmp_path = map_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json.dump(existing, f, ensure_ascii=False, indent=2)
            _os.replace(tmp_path, map_path)
            logger.info(f"article_map 已更新: {map_path} ({len(existing)} 部法律)")
        except Exception as e:
            logger.warning(f"article_map 更新失败（相邻扩展将不生效）: {e}")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def validate(file_path: str) -> tuple[bool, str]:
        """上传前校验文件合法性"""
        path = Path(file_path)
        ext = path.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return False, f"不支持的文件格式: {ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}"
        if not path.exists():
            return False, "文件不存在"
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            return False, f"文件过大: {size_mb:.1f}MB（限制 {MAX_FILE_SIZE_MB}MB）"
        return True, ""

    def _parse_file(self, file_path: str, ext: str) -> str:
        if ext == ".pdf":
            return self._pdf_parser.parse(file_path)
        elif ext == ".docx":
            return self._docx_parser.parse(file_path)
        elif ext == ".txt":
            # 中文法律文档常见 GBK/GB2312 编码，先试 UTF-8，失败回退 GBK
            try:
                return Path(file_path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return Path(file_path).read_text(encoding="gbk")
        else:
            raise ValueError(f"不支持的文件类型: {ext}")

    def ingest_text(
        self,
        title: str,
        text: str,
        doc_type: str = "law",
        source: str = "",
        effective_date: str | None = None,
        force: bool = False,
        status: str = "active",
    ) -> int:
        """直接入库纯文本（无需落盘文件）。

        与 run() 走同一条「清洗 → 分块 → embedding → insert_chunks」链路。

        Args:
            title          : 文档标题（同时用于增量去重，按标题精确匹配）
            text           : 已清洗 / 未清洗的正文
            doc_type       : 文档类型
            source         : 来源标识（如 flk.npc.gov.cn）
            effective_date : 生效日期（ISO 字符串或 None）
            force          : 已存在时是否删除旧文档后重建
            status         : 效力状态（active/repealed/revised/pending）

        Returns:
            0  -> 已存在且非强制（跳过）
            >0 -> 写入的文本块数量
        """
        if not text or len(text) < 20:
            raise ValueError(f"文本过短（{len(text)} 字符）")
        cleaned = self._cleaner.clean(text)
        if not cleaned or len(cleaned) < 20:
            raise ValueError("清洗后文本过短")

        existing = self._store.get_document_id_by_title(title, status=status)
        if existing and not force:
            logger.info(f"[ingest] 跳过(已存在): {title}")
            return 0
        if existing and force:
            logger.info(f"[ingest] 强制重建，删除旧文档: {title}")
            self._store.delete_document(existing)

        doc_id = self._store.ensure_document(
            doc_type=doc_type, title=title, source=source,
            effective_date=effective_date, status=status,
        )
        chunks = self._split_paragraphs(cleaned, doc_id, doc_type=doc_type, title=title)
        if not chunks:
            raise ValueError("分块结果为空")

        for i in range(0, len(chunks), self._embedder.batch_size):
            batch = chunks[i : i + self._embedder.batch_size]
            embeddings = self._embedder.embed_documents([c["content"] for c in batch])
            for c, emb in zip(batch, embeddings):
                c["embedding"] = emb
            self._store.insert_chunks(batch, embedding_model=self._embedder.model)
        self._rebuild_article_map(chunks)
        logger.info(f"[ingest] 写入完成: {title} → {len(chunks)} 块")
        return len(chunks)

    @staticmethod
    def _split_paragraphs(
        text: str,
        doc_id: str,
        doc_type: str = "law",
        max_chars: int = 500,
        title: str | None = None,
    ) -> list[dict]:
        """按文档类型差异化切分文本为块

        - 条文体（law / regulation / constitution / supervision）：以「第X条」
          为天然边界，每个条文独立成块（核心修复：此前按段落切分，条文间
          无空行时多个「第X条」会被糅进同一块）。超长条文再按句号拆分，
          且每个续块都保留条号前缀以便引用追溯。
        - 非条文体（judicial_interpretation / case）：按自然段切分（叙事文/
          解释文无「第X条」结构，不做条文/句子硬拆分，避免切碎语义脉络）。
        - 全文模式：doc_type 位于 FULLTEXT_DOC_TYPES 时，整篇作为一个
          chunk（直接全文召回，不切分）；仅当整篇超长时才保底按句号拆分。
        每个块会带上 law_name（文档标题）与 article_range（解析出的条号），
        供检索结果的「引用条文」展示使用。
        """
        # 全文模式：整篇一个 chunk，直接全文召回
        if doc_type in FULLTEXT_DOC_TYPES:
            chunks = _split_fulltext(text, doc_id, doc_type, max_chars=max_chars)
        # 条文体：按「第X条」切分（法律/行政法规/宪法/监察法规）
        elif doc_type in ("law", "regulation", "constitution", "supervision"):
            chunks = _split_article_paragraphs(text, doc_id, doc_type, max_chars=max_chars)
        # 非条文体（judicial_interpretation / case）：按自然段切分
        else:
            chunks = _split_paragraph_docs(text, doc_id, doc_type, max_chars=max_chars)

        # 补充法律引用字段到 metadata，供检索结果「引用条文」展示
        # （检索器从 document_chunks.metadata JSONB 读取 law_name / article_range）
        law_name = (title or "").strip()
        for i, c in enumerate(chunks):
            meta = c.setdefault("metadata", {})
            meta["law_name"] = law_name
            meta["article_range"] = _extract_article_range(c["content"])
            meta["paragraph_index"] = i
        return chunks


def _split_article_paragraphs(
    text: str, doc_id: str, doc_type: str, max_chars: int = 500
) -> list[dict]:
    """条文体（law/interpretation/regulation）按「第X条」边界切分"""
    chunks: list[dict] = []
    segments = _split_by_articles(text)

    for seg in segments:
        if len(seg) <= max_chars:
            chunks.append({
                "doc_id": doc_id,
                "chunk_type": "article",
                "content": seg,
                "metadata": {"raw": seg, "doc_type": doc_type},
            })
            continue

        # 超长条文按句号拆分，每个续块都以条号开头，保证引用可追溯
        head_m = _ARTICLE_RE.match(seg)
        head = head_m.group(0) if head_m else ""
        body = seg[len(head):].strip() if head else seg
        sentences = body.split("。")
        buf = head
        for s in sentences:
            s = s.strip()
            if not s:
                continue  # 跳过句号产生的空串（如段落以"。"结尾）
            s += "。"
            if len(buf) + len(s) > max_chars and buf.strip():
                chunks.append({
                    "doc_id": doc_id,
                    "chunk_type": "article",
                    "content": buf.strip(),
                    "metadata": {"raw": seg[:200], "doc_type": doc_type},
                })
                buf = head  # 续块重新以条号开头
            buf += s
        if buf.strip():
            chunks.append({
                "doc_id": doc_id,
                "chunk_type": "article",
                "content": buf.strip(),
                "metadata": {"raw": seg[:200], "doc_type": doc_type},
            })
    return chunks


def _split_paragraph_docs(
    text: str, doc_id: str, doc_type: str, max_chars: int = 500
) -> list[dict]:
    """非条文体（interpretation / case）：叙事/解释文，无「第X条」结构。

    按自然段切分，不做条文/句子硬拆分（避免切碎案件事实、裁判理由的
    连续性）。仅当单个段落超长时才按句号拆分。
    """
    chunks: list[dict] = []
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not paragraphs and (text or "").strip():
        paragraphs = [text.strip()]

    for para in paragraphs:
        if len(para) <= max_chars:
            chunks.append({
                "doc_id": doc_id,
                "chunk_type": doc_type,
                "content": para,
                "metadata": {"raw": para, "doc_type": doc_type},
            })
            continue

        # 超长段落按句号拆分（保底，不增加续块前缀）
        sentences = para.split("。")
        buf = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            s += "。"
            if len(buf) + len(s) > max_chars and buf:
                chunks.append({
                    "doc_id": doc_id,
                    "chunk_type": doc_type,
                    "content": buf.strip(),
                    "metadata": {"raw": para[:200], "doc_type": doc_type},
                })
                buf = s
            else:
                buf += s
        if buf.strip():
            chunks.append({
                "doc_id": doc_id,
                "chunk_type": doc_type,
                "content": buf.strip(),
                "metadata": {"raw": para[:200], "doc_type": doc_type},
            })
    return chunks


def _split_fulltext(
    text: str, doc_id: str, doc_type: str, max_chars: int = 500
) -> list[dict]:
    """全文模式：整篇文档作为单个 chunk，直接全文召回不切分。

    仅当整篇超过 max_chars 时才保底按句号拆分（embedding 有长度上限）。
    """
    content = (text or "").strip()
    if not content:
        return []
    if len(content) <= max_chars:
        return [{
            "doc_id": doc_id,
            "chunk_type": doc_type,
            "content": content,
            "metadata": {"raw": content, "doc_type": doc_type},
        }]
    # 超长整篇保底拆分
    return _split_paragraph_docs(text, doc_id, doc_type, max_chars=max_chars)


def _split_by_articles(text: str) -> list[str]:
    """按「第X条」边界把文本切为条文段列表（含前导非条文段，如序言）。

    找不到条文标记时回退为按空行切段，避免整篇被当做一个块。
    """
    text = (text or "").strip()
    if not text:
        return []
    matches = list(_ARTICLE_RE.finditer(text))
    if not matches:
        return [p.strip() for p in text.split("\n\n") if p.strip()] or [text]

    segments: list[str] = []
    prev = 0
    for m in matches:
        lead = text[prev:m.start()].strip()
        if lead:
            segments.append(lead)
        prev = m.start()
    tail = text[prev:].strip()
    if tail:
        segments.append(tail)
    return segments
