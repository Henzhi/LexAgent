"""BM25 关键词检索器（rank-based，不参与分数融合）。

设计要点（对齐"BM25 不要按照分数检索，就按照返回顺序"）:
  - 用 jieba 对 chunks 全文做中文分词，构建 BM25Okapi 索引
  - search() 返回按 BM25 排名排序的 RetrievedDoc 列表
  - score 字段仅填充"排名倒数"作为排序占位，融合层用 RRF（只看排名）而非分数
  - 与向量精确检索完全解耦，只负责关键词召回一路

用法:
  from src.rag.bm25_retriever import Bm25Retriever
  bm25 = Bm25Retriever(store)
  bm25.load_index()          # 首次构建（51348 chunks 约 10-20s）
  docs = bm25.search("刑法第二十条", top_k=10)
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 分词停顿词（法律领域高频但无区分度）
#
# ⚠️ 这张表只放**真正的虚词**。BM25 靠 IDF 自动压制高频词，人工停用词的
#    边际收益很小；反过来，误删一个**有区分度**的词是实打实的召回损失，
#    且极隐蔽——索引能建、查询能跑，只是「某些问题搜不到」。
#
#    历史 Bug：「万元」被当作无区分度词过滤掉，金额类查询（"赔偿标准是多少
#    万元"）因此丢失关键 token。
#    另：「年月日」也在原表里，但 jieba 会把日期切成 ['2023','年','1','月',
#    '1','日']，"年月日"作为整体**从未被产出过**——属死代码，顺带清理。
#    回归测试见 tests/test_bm25_retriever.py。
#
#    待评测确认（高频但有语义价值，暂不过滤，勿拍脑袋增删）：
#      不得 / 应当 / 可以 / 规定 —— 法律模态词，「用人单位不得解除劳动合同」
#        里「不得」就是核心语义；是否过滤应由评测数据决定，不在拍脑袋之列
_STOPWORDS = {
    # 结构助词与连词（无实义）
    "的",
    "了",
    "和",
    "是",
    "在",
    "与",
    "及",
    "或",
    "等",
    "对",
    "为",
    "由",
    "之",
    "其",
    "该",
    "并",
    "也",
    "而",
    "但",
    "以及",
    # 法律文本高频引导词（法条中几乎句句出现，无区分度）
    "按照",
    "根据",
    "依照",
    "本条",
    "第一款",
    # 法名前缀：归一化，让「中华人民共和国劳动合同法」与「劳动合同法」等价
    "中华人民共和国",
}


def _tokenize(text: str) -> list[str]:
    import jieba

    text = re.sub(r"[\s\r\n\t《》（）()【】\[\]\"'“”‘’·,—。；：！？、]+", " ", text or "")
    tokens = []
    for w in jieba.cut(text):
        w = w.strip()
        if not w or w in _STOPWORDS:
            continue
        tokens.append(w)
    return tokens


# ---------------------------------------------------------------------------
# 碎片 chunk 过滤（2026-08-29 基线评测发现，见 evaluation/data/lexeval/results/）
#
# 库里存在 ~3400 个「第九条」「第十七条、」这类纯条号引用碎片（占 6.4%）：
# 切分器把条文中的并列条号引用切成了独立 chunk。它们无任何语义内容，却在
# 几百部法律中大量重复（DF 极高），且文档极短——BM25 的长度归一化会让短
# 文档占便宜，于是这些碎片**系统性挤占 top-k**：查「刑法第二十条」，top5
# 全是信托法/关税法/专利法实施细则的「第二十条」碎片，目标条文反被淹没。
#
# 判定：去掉条号字符与标点后，剩余实质内容 ≤ 6 字视为碎片。
# 阈值 6 经全库校准：纯条号碎片（剩 0~2 字，~1400 个）全数清除；3~6 字区间
# 保留的是「第一百四十九条　生产、销售本节」这类长条文切片的真实片段。
# 若放到 10 会误杀「正当防卫不负刑事责任」这类短而完整的表述。
# 只在检索层过滤（不动 DB、可回退）；入库侧过滤与 DB 清理另行评估。
_FRAGMENT_PATTERN = re.compile(r"[第零一二三四五六七八九十百千条款项章节但书0-9、，。；：（）()．.\s—\-「」『』《》]")
_FRAGMENT_THRESHOLD = 6


def _is_fragment(text: str) -> bool:
    """是否为无实质内容的条号引用碎片（如「第九条」「第十七条、」）。"""
    return len(_FRAGMENT_PATTERN.sub("", text or "")) <= _FRAGMENT_THRESHOLD


# 法名加权（2026-08-29 基线评测发现）：
# 法名只拼一次时，其 DF = 该法的 chunk 数，IDF 被稀释到低于条号 token
# （实测 53235 chunks 下「刑法」idf≈3.53 < 「第二十条」≈4.11），叠加长度
# 归一化惩罚，导致「刑法第二十条」被其他法律的「第二十条」淹没。
# 把法名重复 LAW_NAME_BOOST 次拼入索引文本，提高本法 chunk 内法名 token
# 的 TF——注意 BM25 的 DF 按包含该词的文档数计，文档内重复不改变 DF，
# 所以这是纯增益的加权方式，不会反过来稀释其他词。
LAW_NAME_BOOST = 3


class Bm25Retriever:
    """Python 端 BM25 检索器（jieba + rank-bm25）。"""

    def __init__(self, store, index_path: Optional[str] = None):
        """
        Args:
            store: PgvectorStore 实例（用于读取 chunks 构建索引）
            index_path: 索引缓存路径（可选，后续可持久化）
        """
        self._store = store
        self._index_path = index_path
        self._bm25 = None
        self._chunks: list[dict] = []  # {content, metadata}
        # 保护懒加载与索引读取（多请求并发时避免重复构建 / 读写竞争）
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------

    def load_index(self, force: bool = False) -> None:
        """从 DB 加载全部 chunks 并构建 BM25 索引。

        关键: chunk content 只有条文正文、不含法名（法名仅在 metadata）。
        向量索引无法靠"法名"匹配，但 BM25 可以——这里把 law_name 拼进
        索引文本（"法名 第X条 正文"），让 BM25 对"法名+关键词"查询有
        精确匹配能力（与向量语义检索形成互补）。
        """
        with self._lock:
            if self._bm25 is not None and not force:
                return
            from rank_bm25 import BM25Okapi

            self._store.ensure_tables()
            rows = self._store.fetch_all_active_chunks()
            self._chunks = [
                {"content": r[0] or "", "metadata": r[1] or {}}
                for r in rows
                # 碎片 chunk（纯条号引用）无检索价值且挤占 top-k，见上方说明
                if not _is_fragment(r[0] or "")
            ]
            # 法名拼入索引文本（重复 LAW_NAME_BOOST 次加权，见上方说明）。
            # 法名 token 在本法 chunk 内 TF=BOOST，跨 chunk DF 不变。
            tokenized = []
            for c in self._chunks:
                meta = c["metadata"]
                law_name = meta.get("law_name", "")
                prefix = f"{law_name} " * LAW_NAME_BOOST if law_name else ""
                full_text = f"{prefix}{c['content']}"
                tokenized.append(_tokenize(full_text))
            self._bm25 = BM25Okapi(tokenized)
            logger.info(f"BM25 索引就绪: {len(self._chunks)} 个 chunks（法名已拼入）")

    def is_ready(self) -> bool:
        return self._bm25 is not None

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 10, **kwargs) -> list:
        from .retriever import RetrievedDoc

        if self._bm25 is None:
            self.load_index()
        with self._lock:
            tokens = _tokenize(query)
            if not tokens:
                return []
            scores = self._bm25.get_scores(tokens)
            # 按 BM25 分数排名取 top_k（排名即顺序；score 仅作排序，不参与融合）
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            docs: list[RetrievedDoc] = []
            # 同一条文（长条多段/交叉引用）会切出多个 chunk，只保留排名最高的
            # 一个，空位由后续条文补足——否则 top-k 被同条冗余占满（基线评测中
            # 「专利法实施细则 第四十二条」12 个 chunk 占满 top5）。
            # article_range 为空的 chunk（总则等）不参与去重。
            seen: set[tuple[str, str]] = set()
            for rank, idx in enumerate(order, 1):
                meta = self._chunks[idx]["metadata"]
                law_name = meta.get("law_name", "")
                article_range = meta.get("article_range", "")
                if article_range:
                    key = (law_name, article_range)
                    if key in seen:
                        continue
                    seen.add(key)
                docs.append(
                    RetrievedDoc(
                        content=self._chunks[idx]["content"],
                        score=1.0 / rank,  # 排名倒数，仅占位
                        law_name=law_name,
                        chapter=meta.get("chapter", ""),
                        section=meta.get("section", ""),
                        article_range=article_range,
                        chunk_type=meta.get("chunk_type", ""),
                    )
                )
                if len(docs) >= top_k:
                    break
            return docs
