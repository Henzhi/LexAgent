"""国家法律法规数据库（flk.npc.gov.cn）爬虫。

数据源 : 全国人大官方「国家法律法规数据库」 https://flk.npc.gov.cn
合规提示: 仅用于个人学习 / 研究；请控制请求频率，遵守目标网站 robots.txt 与版权。

接口（基于 2026-05 实测验证的「二期 API」；旧 /api/、/api/detail 已于 2025-08 废弃）:
  列表 : POST https://flk.npc.gov.cn/law-search/search/list
         body(JSON): searchRange(1=标题,2=正文), searchContent(关键词),
                     searchType(1=精确,2=模糊), sxx([3]=现行有效),
                     flfgCodeId(int[], 分类码), pageNum, pageSize(≤100)
         返回: {code, total, rows:[{bbbs, title(含<em>高亮), flxz, sxrq, gbrq, ...}]}
  下载 : GET  https://flk.npc.gov.cn/law-search/download/pc?format=docx&bbbs={bbbs}&fileId=
         返回: {code, data:{url(签名外链, ~1h有效), urlIn(内网, 忽略)}}
         再用 data.url 直接下载 docx/pdf 二进制
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.knowledge.ingestion.docx_parser import DocxParser
from src.knowledge.ingestion.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)

API_BASE = "https://flk.npc.gov.cn"
LIST_URL = f"{API_BASE}/law-search/search/list"
DOWNLOAD_URL = f"{API_BASE}/law-search/download/pc"

# doc_type -> (flfgCodeId 分类码列表, 输出子目录名)
# doc_type 使用 flk 顶级分类规范值（见 src.knowledge.doc_types）：
# 分类码来自 2026 实测：宪法 100；法律 110/120/130/140/150/160/180；
# 行政法规 210；监察法规 220；地方法规 230；司法解释 320/340
TYPE_MAP: dict[str, tuple[list[int], str]] = {
    "constitution": ([100], "constitution"),                      # 宪法
    "law": ([110, 120, 130, 140, 150, 160, 180], "laws"),          # 法律 / 法律解释
    "regulation": ([210], "regulations"),                         # 行政法规
    "supervision": ([220], "supervision_regulations"),            # 监察法规
    "judicial_interpretation": ([320, 340], "judicial_interpretations"),  # 司法解释
    "local_regulation": ([230], "local_regulations"),             # 地方性法规
}

# 该数据源不支持的类型（案例 / 裁判文书不在 flk）
UNSUPPORTED_TYPES = {"case"}

# 入库来源标识（写入 pgvector documents.source 字段）
SOURCE = "flk.npc.gov.cn"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json;charset=utf-8",
    "Accept": "application/json, text/plain, */*",
}

_DEFAULT_LAW_DATA = Path(__file__).resolve().parents[3] / "LawData"

_tag_re = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_title(raw: str) -> str:
    """去除标题中的 HTML 高亮标签并反转义实体。"""
    if not raw:
        return ""
    return html.unescape(_tag_re.sub("", raw)).strip()


@dataclass
class CrawlResult:
    """单次爬取结果统计"""
    total: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)  # 相对 LawData 的路径


@dataclass
class _ManifestEntry:
    id: str
    title: str
    type: str
    file: str
    crawled_at: str
    effective_date: str = ""
    size: int = 0


class NpcLawCrawler:
    """爬取国家法律法规数据库，清洗后落地到 LawData/<子目录>/。"""

    def __init__(
        self,
        law_data_dir: str | Path = _DEFAULT_LAW_DATA,
        sleep: float = 1.0,
        timeout: int = 30,
    ):
        self.law_data_dir = Path(law_data_dir)
        self.sleep = max(0.0, sleep)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self._docx_parser = DocxParser()
        self._cleaner = TextCleaner()
        # pgvector 入库相关（懒加载，仅在 store 含 pg 时构建）
        self._pg_store = None
        self._pg_pipeline = None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def crawl(
        self,
        doc_type: str = "law",
        keyword: str = "",
        limit: int = 50,
        force: bool = False,
        subdir: str = "",
        store: str = "pg",
        progress_cb: callable | None = None,
        sxx: list[str] | None = None,
    ) -> CrawlResult:
        """爬取并落地。

        Args:
            doc_type: law/regulation/judicial/local/constitution/supervision/all
            keyword : 标题模糊搜索关键词（空=该类型全部）
            limit   : 最多爬取条数，0=不限
            force   : 强制重爬已存在的文档
            subdir  : 覆盖输出子目录名（默认按 doc_type 自动）
            store   : 输出目标，可组合:
                      "pg"   -> 直接写入 pgvector（推荐，默认）
                      "txt"  -> 落地到 LawData/<子目录>/*.txt（原始文本存档）
                      "both" -> 两者都做
            progress_cb: 每处理一条后回调 CrawlResult，用于进度上报
            sxx     : 效力状态过滤（flk 码），None=仅现行有效["3"]；
                      可组合如 ["3"]现行有效、["1","3"]现行+已废止、
                      ["1","2","3","4"]全部。
        """
        if doc_type in UNSUPPORTED_TYPES:
            raise ValueError(
                f"数据源 flk.npc.gov.cn 暂不支持类型 '{doc_type}'（案例 / 裁判文书）。"
                "如需案例，请提供其他数据源或扩展爬虫。"
            )

        # 默认仅现行有效（历史行为）；传入 sxx 则按需采集
        status_filter = sxx if sxx else ["3"]

        # 自动分类模式：不指定 doc_type，按每条 flxz 自动判定归属
        if doc_type == "auto":
            return self._crawl_auto(keyword, limit, force, store, progress_cb, status_filter)

        if doc_type == "all":
            merged = CrawlResult()
            for t in TYPE_MAP:
                sub = self._crawl_one(t, keyword, limit, force, "", store, progress_cb, status_filter)
                for k in ("total", "added", "updated", "skipped", "failed"):
                    setattr(merged, k, getattr(merged, k) + getattr(sub, k))
                merged.errors.extend(sub.errors)
                merged.files.extend(sub.files)
            # 注：HNSW 索引增量生效，入库无需 REINDEX（全量重建会锁表）；
            # 如确需整理索引，由调用方显式调用 store.reindex()
            return merged

        res = self._crawl_one(doc_type, keyword, limit, force, subdir, store, progress_cb, status_filter)
        # 注：HNSW 索引增量生效，入库无需 REINDEX（全量重建会锁表）
        return res

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _crawl_one(self, doc_type, keyword, limit, force, subdir, store, progress_cb, status_filter=None) -> CrawlResult:
        code_ids, default_sub = TYPE_MAP.get(doc_type, ([], doc_type))
        out_dir = self.law_data_dir / (subdir or default_sub)
        out_dir.mkdir(parents=True, exist_ok=True)

        sinks = self._parse_store(store)
        do_txt = "txt" in sinks
        do_pg = "pg" in sinks
        if do_pg:
            # 提前构建 pg 连接，使下面的去重检查可用
            self._ensure_pg()

        manifest_before = self._load_manifest(out_dir)
        manifest = dict(manifest_before)
        result = CrawlResult()

        docs = self._fetch_list(code_ids, keyword, limit, status_filter)
        result.total = len(docs)
        logger.info(f"[crawl] type={doc_type} 命中 {len(docs)} 部, 输出目录={out_dir}, 目标={sinks}")

        for item in docs:
            doc_id = str(item.get("bbbs") or "")
            title = _clean_title(item.get("title") or "")
            if not title:
                title = f"doc_{doc_id}"
            effective_date = item.get("sxrq") or item.get("gbrq") or ""
            # pg 列是 DATE，仅当格式为 YYYY-MM-DD 才传入，否则按 NULL 处理
            eff_pg = effective_date if _DATE_RE.match(effective_date) else None

            # 效力状态：flk 列表返回的 sxx 字段（字符串 "1/2/3/4"）
            # 1=已废止 2=已修改 3=现行有效 4=尚未生效
            from src.knowledge.doc_types import status_from_sxx
            status = status_from_sxx(item.get("sxx"))

            # 按 sink 分别判定增量去重。
            # pg 侧按 (title, status) 精确匹配：同一标题不同效力版本（如现行有效
            # 与已废止）视为不同文档，各自入库；同标题同状态才算已存在。
            txt_existed = doc_id in manifest_before
            txt_skip = (not force) and txt_existed
            pg_existed = False
            if do_pg and self._pg_store is not None:
                pg_existed = self._pg_store.get_document_id_by_title(title, status=status) is not None
            pg_skip = (not force) and pg_existed

            # 仅当所有启用的 sink 都已存在时整条跳过；否则各 sink 各自去重写入
            if (not do_txt or txt_skip) and (not do_pg or pg_skip):
                result.skipped += 1
                logger.debug(f"[crawl] 跳过(已存在): {title}")
                continue

            try:
                text = self._fetch_document(doc_id, title)
                if not text or len(text) < 30:
                    raise ValueError("正文为空或过短")

                # 1) 落地 txt（原始文本存档，增量按 manifest/bbbs）
                if do_txt and not txt_skip:
                    rel_path = self._save(out_dir, doc_id, title, text, effective_date)
                    manifest[doc_id] = asdict(_ManifestEntry(
                        id=doc_id, title=title, type=doc_type,
                        file=rel_path, crawled_at=_now(),
                        effective_date=effective_date, size=len(text),
                    ))
                    result.files.append(rel_path)
                    if txt_existed:
                        result.updated += 1
                    else:
                        result.added += 1
                    logger.info(f"[crawl] txt {'更新' if txt_existed else '新增'}: {title}")

                # 2) 入库 pgvector（增量按标题去重）
                if do_pg and not pg_skip:
                    n = self._ingest_pg(doc_type, title, text, eff_pg, force, status)
                    result.files.append(f"pg:{title}")
                    if pg_existed:
                        result.updated += 1
                    else:
                        result.added += 1
                    logger.info(f"[crawl] pg 写入 {n} 块: {title}")

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{title}: {e}")
                logger.warning(f"[crawl] 失败 {title}: {e}")

            self._save_manifest(out_dir, manifest)
            if progress_cb:
                progress_cb(result)
            time.sleep(self.sleep)

        return result

    def _crawl_auto(self, keyword, limit, force, store, progress_cb, status_filter=None) -> CrawlResult:
        """自动分类爬取：全库关键词搜索，按每条记录的 flxz 自动判定 doc_type。

        与 _crawl_one 的区别：
        - 不限定 flfgCodeId，一次搜索覆盖全部类型
        - 每条按 flxz（法律形式）判定归属，写入对应类型的子目录 / 入库 doc_type
        - 无法识别的类型记录到 result.errors，避免静默错分
        """
        from src.knowledge.doc_types import doc_type_from_flxz, normalize_doc_type

        out_dir = self.law_data_dir / "auto"
        out_dir.mkdir(parents=True, exist_ok=True)

        sinks = self._parse_store(store)
        do_txt = "txt" in sinks
        do_pg = "pg" in sinks
        if do_pg:
            self._ensure_pg()

        manifest_before = self._load_manifest(out_dir)
        manifest = dict(manifest_before)
        result = CrawlResult()

        # 全库搜索（flfgCodeId 留空 = 全部类型）
        docs = self._fetch_list([], keyword, limit, status_filter)
        logger.info(f"[crawl] auto 命中 {len(docs)} 条，按 flxz 自动分类")

        for item in docs:
            doc_id = str(item.get("bbbs") or "")
            title = _clean_title(item.get("title") or "")
            if not title:
                title = f"doc_{doc_id}"

            # 自动分类：flxz 法律形式 -> 规范 doc_type
            flxz = item.get("flxz") or ""
            doc_type = doc_type_from_flxz(flxz)
            if not doc_type:
                result.failed += 1
                result.errors.append(f"{title}: 无法识别法律形式 flxz={flxz!r}，跳过")
                logger.warning(f"[crawl] auto 无法识别 flxz={flxz!r}: {title}")
                continue
            doc_type = normalize_doc_type(doc_type)

            effective_date = item.get("sxrq") or item.get("gbrq") or ""
            eff_pg = effective_date if _DATE_RE.match(effective_date) else None
            from src.knowledge.doc_types import status_from_sxx
            status = status_from_sxx(item.get("sxx"))

            # 自动识别已入库：pg 按 (title, status) 精确去重，同标题不同效力
            # 版本各自保留；txt 侧用 flk 文档 id (bbbs) 去重
            txt_existed = doc_id in manifest_before
            txt_skip = (not force) and txt_existed
            pg_existed = False
            if do_pg and self._pg_store is not None:
                pg_existed = self._pg_store.get_document_id_by_title(title, status=status) is not None
            pg_skip = (not force) and pg_existed

            # 仅当所有启用的 sink 都已存在时整条跳过；否则各 sink 各自去重写入
            if (not do_txt or txt_skip) and (not do_pg or pg_skip):
                result.skipped += 1
                continue

            try:
                text = self._fetch_document(doc_id, title)
                if not text or len(text) < 30:
                    raise ValueError("正文为空或过短")

                if do_txt and not txt_skip:
                    rel_path = self._save(out_dir, doc_id, title, text, effective_date)
                    manifest[doc_id] = asdict(_ManifestEntry(
                        id=doc_id, title=title, type=doc_type,
                        file=rel_path, crawled_at=_now(),
                        effective_date=effective_date, size=len(text),
                    ))
                    result.files.append(rel_path)
                    if txt_existed:
                        result.updated += 1
                    else:
                        result.added += 1
                    logger.info(f"[crawl] auto[{doc_type}] txt {'更新' if txt_existed else '新增'}: {title}")

                if do_pg and not pg_skip:
                    n = self._ingest_pg(doc_type, title, text, eff_pg, force, status)
                    result.files.append(f"pg:{title}")
                    if pg_existed:
                        result.updated += 1
                    else:
                        result.added += 1
                    logger.info(f"[crawl] auto[{doc_type}] pg 写入 {n} 块: {title}")

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{title}: {e}")
                logger.warning(f"[crawl] auto 失败 {title}: {e}")

            self._save_manifest(out_dir, manifest)
            if progress_cb:
                progress_cb(result)
            time.sleep(self.sleep)

        return result

    @staticmethod
    def _parse_store(store: str) -> set[str]:
        s = (store or "txt").lower()
        if "both" in s.split(","):
            return {"txt", "pg"}
        sinks = {t for t in re.split(r"[ ,+]+", s) if t in ("txt", "pg")}
        return sinks or {"txt"}

    def _ensure_pg(self) -> None:
        """懒加载 pgvector store + ingestion pipeline（仅在 store 含 pg 时构建）。"""
        if self._pg_pipeline is not None:
            return
        from src.config import (
            EMBED_BATCH_SIZE,
            EMBED_MAX_RETRIES,
            EMBED_MODEL,
            PG_CONN,
            PG_ENABLED,
        )
        if not PG_ENABLED:
            raise RuntimeError(
                "PG_ENABLED 未开启，无法写入 pgvector。请在 .env 设置 PG_ENABLED=true "
                "并先执行 `docker compose up -d` 初始化数据库与表结构。"
            )
        from src.knowledge.pgvector_store import PgvectorStore
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        from src.embedding.factory import create_embedding_backend
        from src.llm.adapter import EmbeddingAdapter

        store = PgvectorStore(PG_CONN)
        store.ensure_tables()
        embedder = EmbeddingAdapter(
            create_embedding_backend(
                None,
                model=EMBED_MODEL,
                batch_size=EMBED_BATCH_SIZE,
                max_retries=EMBED_MAX_RETRIES,
            )
        )
        self._pg_store = store
        self._pg_pipeline = IngestionPipeline(store, embedder)

    def _ingest_pg(self, doc_type, title, text, effective_date, force, status="active") -> int:
        return self._pg_pipeline.ingest_text(
            title, text, doc_type=doc_type, source=SOURCE,
            effective_date=effective_date or None, force=force, status=status,
        )

    def _fetch_list(self, code_ids: list[int], keyword: str, limit: int, status_filter: list[str] | None = None) -> list[dict]:
        collected: list[dict] = []
        page = 1
        size = 100  # 二期 API 最大 100
        while True:
            body = {
                "searchRange": 1,            # 1=标题检索
                "searchContent": keyword,    # 空=该分类全部
                "searchType": 2,             # 2=模糊
                # 效力状态过滤：1=已废止 2=已修改 3=现行有效 4=尚未生效
                "sxx": [int(x) for x in (status_filter or ["3"])],
                "sxrq": [], "gbrq": [], "gbrqYear": [],
                "flfgCodeId": code_ids,      # 按分类过滤（空=全部）
                "zdjgCodeId": [],
                "xgzlSearch": False,
                "pageNum": page,
                "pageSize": size,
            }
            try:
                r = self.session.post(
                    LIST_URL, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.error(f"[crawl] 列表请求失败 page={page}: {e}")
                break

            if data.get("code") != 200:
                logger.error(f"[crawl] 列表返回非 200: {data.get('msg')}")
                break

            items = data.get("rows") or []
            if not items:
                break
            collected.extend(items)
            if limit and len(collected) >= limit:
                collected = collected[:limit]
                break
            if len(items) < size:
                break
            page += 1
            time.sleep(self.sleep)
        return collected

    def _fetch_document(self, doc_id: str, title: str) -> str:
        """下载正文（优先 docx，失败回退 pdf）并返回清洗后的纯文本。"""
        last_err = None
        for fmt in ("docx", "pdf"):
            try:
                r = self.session.get(
                    DOWNLOAD_URL,
                    params={"format": fmt, "bbbs": doc_id, "fileId": ""},
                    timeout=self.timeout,
                )
                r.raise_for_status()
                payload = r.json()
                if payload.get("code") != 200:
                    raise RuntimeError(payload.get("msg", "下载接口异常"))
                signed_url = (payload.get("data") or {}).get("url") or ""
                if not signed_url:
                    raise RuntimeError("未返回签名下载地址")
                d = self.session.get(signed_url, timeout=self.timeout)
                d.raise_for_status()
                content = d.content
                if not content or len(content) < 100:
                    raise RuntimeError("下载内容为空")
            except Exception as e:
                last_err = e
                logger.warning(f"[crawl] {fmt} 下载失败 {title}: {e}")
                continue

            try:
                if fmt == "pdf":
                    import os
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                        tf.write(content)
                        tmp = tf.name
                    try:
                        from src.knowledge.ingestion.pdf_parser import PDFParser
                        raw = PDFParser().parse(tmp)
                    finally:
                        os.unlink(tmp)
                else:
                    raw = self._docx_parser.parse_bytes(content)
                return self._cleaner.clean(raw)
            except Exception as e:
                last_err = e
                logger.warning(f"[crawl] {fmt} 解析失败 {title}: {e}")
                continue

        raise RuntimeError(f"docx/pdf 均获取失败: {last_err}")

    def _save(self, out_dir: Path, doc_id: str, title: str, text: str, effective_date: str) -> str:
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title).strip() or f"doc_{doc_id}"
        if len(safe) > 120:
            safe = safe[:120]
        file_path = out_dir / f"{safe}.txt"
        # 第一行写法律标题，便于 parser.py 解析；随后是清洗后的正文
        file_path.write_text(f"{title}\n\n{text}\n", encoding="utf-8")
        return str(file_path.relative_to(self.law_data_dir))

    # ---- manifest（增量记录） ----
    def _manifest_path(self, out_dir: Path) -> Path:
        return out_dir / ".crawl_manifest.json"

    def _load_manifest(self, out_dir: Path) -> dict:
        p = self._manifest_path(out_dir)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                logger.warning(f"[crawl] manifest 解析失败，视为空: {p}")
        return {}

    def _save_manifest(self, out_dir: Path, manifest: dict) -> None:
        p = self._manifest_path(out_dir)
        p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
