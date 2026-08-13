"""
法律文档切分器。

采用 "法条级 + 层次上下文" 混合切分策略：

┌──────────────────────────────────────────────────┐
│  策略                                           │
│                                                 │
│  1. 以 "条（Article）" 为最小切分单元             │
│  2. 过短的连续条文会合并为一个 chunk               │
│  3. 每个 chunk 携带完整层次元数据                  │
│     (法律名 → 编 → 章 → 节 → 条号范围)            │
│  4. 可选：额外生成章级摘要 chunk                   │
│     (便于粗粒度检索后精排)                         │
└──────────────────────────────────────────────────┘

输出为 LangChain Document 列表，方便直接灌入向量库。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from langchain_core.documents import Document

from .parser import LawDocument, Article, Chapter


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class ChunkConfig:
    """切分配置"""
    min_chunk_chars: int = 50          # 短于该值的单个 chunk 会和相邻合并
    max_chunk_chars: int = 1500        # 单 chunk 最大长度（超过则强制拆分）
    # 默认不合并短条文：法律条文是最小语义单元，合并后整块召回无法定位
    # 到具体条文，且 embedding 语义被稀释（"算哪一条"问题）。
    # 确需合并时须开启，且每条会保留条号前缀以便追溯。
    merge_short_articles: bool = False
    max_merge_articles: int = 3        # 最多合并条数（防止"第一条至第N条"大杂烩）
    add_chapter_summary: bool = True   # 是否为每章生成摘要 chunk
    context_prefix_template: str = (
        '【{law_name}】{part}{chapter}{section}'
    )
    article_separator: str = '\n'


# ---------------------------------------------------------------------------
# 上下文构建
# ---------------------------------------------------------------------------

def _build_article_context(
    doc: LawDocument,
    article: Article,
    chapter_title: str = '',
    section_title: str = '',
    part_title: str = '',
) -> dict[str, str]:
    """构建单条条文的层次上下文元数据"""
    return {
        'law_name': doc.title,
        'part': part_title,
        'chapter': chapter_title,
        'section': section_title,
        'article_number': article.number,
        'article_number_int': str(article.number_int),
        'article_index': str(article.index),
    }


def _build_context_prefix(meta: dict[str, str], cfg: ChunkConfig) -> str:
    """根据元数据生成上下文前缀，嵌入 chunk 文本中"""
    law = meta.get('law_name', '')
    part = f'／{meta["part"]}' if meta.get('part') else ''
    chapter = f'／{meta["chapter"]}' if meta.get('chapter') else ''
    section = f'／{meta["section"]}' if meta.get('section') else ''
    return cfg.context_prefix_template.format(
        law_name=law, part=part, chapter=chapter, section=section,
    )


def _make_article_range_text(articles: list[Article]) -> str:
    """生成条文范围描述，如 '第一条至第三条'"""
    if len(articles) == 1:
        return f'第{articles[0].number}条'
    return f'第{articles[0].number}条至第{articles[-1].number}条'


# ---------------------------------------------------------------------------
# 切分器
# ---------------------------------------------------------------------------

class LawChunker:
    """法律文档切分器"""

    def __init__(self, config: Optional[ChunkConfig] = None):
        self.cfg = config or ChunkConfig()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def chunk_document(self, doc: LawDocument) -> list[Document]:
        """将一部法律切分为 LangChain Document 列表"""
        chunks: list[Document] = []

        # 获取所有条文的层次信息
        article_metas = self._collect_article_metas(doc)

        # 按法条切分 + 合并短法条
        article_chunks = self._chunk_articles(article_metas, doc)
        chunks.extend(article_chunks)

        # 可选：章级摘要 chunk
        if self.cfg.add_chapter_summary:
            chapter_chunks = self._chunk_chapter_summaries(doc)
            chunks.extend(chapter_chunks)

        return chunks

    def chunk_documents(self, docs: list[LawDocument]) -> list[Document]:
        """批量切分"""
        all_chunks = []
        for doc in docs:
            all_chunks.extend(self.chunk_document(doc))
        return all_chunks

    # ------------------------------------------------------------------
    # 元数据收集
    # ------------------------------------------------------------------

    def _collect_article_metas(self, doc: LawDocument) -> list[dict]:
        """收集每条条文 + 其层次上下文"""
        result: list[dict] = []

        if doc.parts:
            for part in doc.parts:
                for ch in part.chapters:
                    result.extend(_walk_sections(ch, doc, part.title))
        elif doc.chapters:
            for ch in doc.chapters:
                result.extend(_walk_sections(ch, doc, ''))
        else:
            for art in doc.articles:
                result.append(_build_article_context(doc, art))
        return result

    # ------------------------------------------------------------------
    # 法条 chunk
    # ------------------------------------------------------------------

    def _chunk_articles(
        self, metas: list[dict], doc: LawDocument
    ) -> list[Document]:
        """按法条切分，合并过短的法条"""
        chunks: list[Document] = []
        buffer_texts: list[str] = []
        buffer_metas: list[dict] = []
        buffer_chars = 0

        def _flush():
            nonlocal buffer_chars
            if not buffer_texts:
                return
            # 合并时每条条文强制保留条号前缀（如"第三条 因行贿……"），
            # 保证召回后能追溯"这是哪一条"，避免多条正文无法区分
            merged_text = self.cfg.article_separator.join(
                f'第{doc.articles[int(m["article_index"]) - 1].number}条 {t}'
                for m, t in zip(buffer_metas, buffer_texts)
            )
            # 以第一条的上下文件为主体元数据
            first_meta = buffer_metas[0]
            prefix = _build_context_prefix(first_meta, self.cfg)
            page_content = f'{prefix}\n{merged_text}'

            # 构建合并元数据
            merged_meta = dict(first_meta)
            merged_meta['article_range'] = _make_article_range_text([
                doc.articles[int(m['article_index']) - 1] for m in buffer_metas
            ])
            merged_meta['chunk_type'] = 'article'
            merged_meta['article_count'] = str(len(buffer_metas))

            chunks.append(Document(page_content=page_content, metadata=merged_meta))
            buffer_texts.clear()
            buffer_metas.clear()
            buffer_chars = 0

        for meta in metas:
            idx = int(meta['article_index']) - 1
            article = doc.articles[idx]
            text = article.content

            # 超长法条：先 flush 缓冲区，再单独处理
            if len(text) > self.cfg.max_chunk_chars:
                _flush()
                prefix = _build_context_prefix(meta, self.cfg)
                meta['article_range'] = f'第{article.number}条'
                meta['chunk_type'] = 'article'
                meta['article_count'] = '1'
                chunks.append(Document(
                    page_content=f'{prefix}\n{text}',
                    metadata=meta,
                ))
                continue

            # 能否并入当前缓冲区（默认 merge_short_articles=False → 不合并，每条独立成块）：
            # 1) 开启了短条文合并
            # 2) 缓冲区非空
            # 3) 与缓冲首条同章（禁止跨章糅合）
            # 4) 未达合并条数上限（防止"第一条至第N条"大杂烩）
            can_merge = (
                self.cfg.merge_short_articles
                and bool(buffer_metas)
                and buffer_metas[0].get('chapter') == meta.get('chapter')
                and len(buffer_metas) < self.cfg.max_merge_articles
            )

            # 缓冲区满了 或 不可合并 → 先 flush
            if buffer_chars + len(text) > self.cfg.max_chunk_chars or (buffer_metas and not can_merge):
                _flush()

            buffer_texts.append(text)
            buffer_metas.append(meta)
            buffer_chars += len(text)

            # 未开启合并：每条立即独立成块
            if not self.cfg.merge_short_articles:
                _flush()
            # 开启合并：当前条文已够长（>= min_chunk_chars）才出块
            elif buffer_chars >= self.cfg.min_chunk_chars:
                _flush()

        _flush()  # 处理末尾残留
        return chunks

    # ------------------------------------------------------------------
    # 章级摘要 chunk
    # ------------------------------------------------------------------

    def _chunk_chapter_summaries(self, doc: LawDocument) -> list[Document]:
        """为每章生成一个摘要性 chunk（包含该章下所有法条的关键信息）"""
        chunks: list[Document] = []

        def _process_chapter(ch: Chapter, part_title: str = '') -> list[Document]:
            result = []
            articles: list[Article] = []

            if ch.sections:
                for sec in ch.sections:
                    articles.extend(sec.articles)
            articles.extend(ch.articles)

            if not articles:
                return result

            # 生成章级摘要：章节标题 + 所有法条的缩略文本
            part_prefix = f'／{part_title}' if part_title else ''
            chapter_header = f'【{doc.title}】{part_prefix}／{ch.title}'
            article_summaries = []
            for art in articles:
                # 取每条的前 100 字作为摘要
                summary = art.content[:100] + ('...' if len(art.content) > 100 else '')
                article_summaries.append(f'第{art.number}条: {summary}')

            page_content = chapter_header + '\n' + '\n'.join(article_summaries)

            meta = {
                'law_name': doc.title,
                'part': part_title,
                'chapter': ch.title,
                'section': '',
                'article_range': _make_article_range_text(articles),
                'article_count': str(len(articles)),
                'chunk_type': 'chapter_summary',
            }
            result.append(Document(page_content=page_content, metadata=meta))
            return result

        if doc.parts:
            for part in doc.parts:
                for ch in part.chapters:
                    chunks.extend(_process_chapter(ch, part.title))
        elif doc.chapters:
            for ch in doc.chapters:
                chunks.extend(_process_chapter(ch))
        return chunks


# ---------------------------------------------------------------------------
# 辅助：章节遍历（内部用）
# ---------------------------------------------------------------------------

def _walk_sections(
    ch: Chapter, doc: LawDocument, part_title: str = ''
) -> list[dict]:
    """遍历章下所有条文，收集元数据"""
    result: list[dict] = []
    if ch.sections:
        for sec in ch.sections:
            for art in sec.articles:
                result.append(_build_article_context(
                    doc, art, ch.title, sec.title, part_title,
                ))
    for art in ch.articles:
        result.append(_build_article_context(
            doc, art, ch.title, '', part_title,
        ))
    return result
