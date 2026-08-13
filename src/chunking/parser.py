"""
法律文档结构解析器。

将原始的法律 .txt 文件解析为结构化的法律体系：
  编 (Part) → 章 (Chapter) → 节 (Section) → 条 (Article)

每一条（Article）是最小的语义单元，保留完整的层次上下文。
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 正则模式
# ---------------------------------------------------------------------------

# 匹配：第X编/章/节/条　标题或内容
_RE_PART    = re.compile(r'^第([一二三四五六七八九十百]+)编\s+(.+)')
_RE_CHAPTER = re.compile(r'^第([一二三四五六七八九十百]+)章\s+(.+)')
_RE_SECTION = re.compile(r'^第([一二三四五六七八九十百]+)节\s+(.+)')
_RE_ARTICLE = re.compile(r'^第([一二三四五六七八九十百千]+)条[\s　]+(.+)')

# 匹配"目录"所在行，遇到目录后跳过直到第一条正文出现
_RE_TOC = re.compile(r'^目\s*录\s*$')

# 匹配主席令等序言行（以特定关键词开头）
_PREAMBLE_KEYWORDS = [
    '中华人民共和国主席令', '全国人民代表大会常务委员会',
    '（', '第', '主席', '委员长',
]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Article:
    """单条法律条文"""
    index: int              # 全局序号，如 1, 2, 3 ...
    number: str             # 中文序号，如 "一"、"十二"、"一百二十三"
    number_int: int         # 整型序号
    text: str               # 正文内容
    content: str = ''       # 完整上下文文本（含后续续行）


@dataclass
class Section:
    """一节"""
    title: str
    articles: list[Article] = field(default_factory=list)


@dataclass
class Chapter:
    """一章"""
    title: str
    sections: list[Section] = field(default_factory=list)
    articles: list[Article] = field(default_factory=list)  # 无节时直接挂条文


@dataclass
class Part:
    """一编（部分法律才有编，如民法典、刑法）"""
    title: str
    chapters: list[Chapter] = field(default_factory=list)


@dataclass
class LawDocument:
    """一部完整的法律"""
    file_path: str
    title: str
    preamble: str = ''          # 序言（主席令、修订历史等）
    parts: list[Part] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)  # 无编时直接挂章
    articles: list[Article] = field(default_factory=list)  # 所有条文的扁平列表


# ---------------------------------------------------------------------------
# 中文数字 → 整数 转换
# ---------------------------------------------------------------------------

_CN_NUM_MAP = {
    '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    '十': 10, '百': 100, '千': 1000,
}


def _cn_to_int(cn: str) -> int:
    """将中文数字字符串转为整数，如 '一百二十三' → 123"""
    total = 0
    base = 1
    for i, ch in enumerate(reversed(cn)):
        val = _CN_NUM_MAP.get(ch)
        if val is None:
            continue
        if val >= 10:
            base = max(base, val)
        else:
            total += val * (base if base >= 10 else 1)
    # 处理 "十"、"十二" 等
    if cn.startswith('十'):
        total += 10
    if cn.endswith('十'):
        total += base * 10
    # 重新计算更稳健的方式
    result = 0
    unit = 1
    i = len(cn) - 1
    while i >= 0:
        ch = cn[i]
        val = _CN_NUM_MAP.get(ch, 0)
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


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------

class LawParser:
    """法律文档解析器"""

    def __init__(self, min_article_chars: int = 10):
        """
        Args:
            min_article_chars: 条文正文最小长度，用于过滤误匹配。
        """
        self.min_article_chars = min_article_chars

    def parse_file(self, file_path: str | Path) -> LawDocument:
        """解析单个法律文件"""
        file_path = Path(file_path)
        raw_text = file_path.read_text(encoding='utf-8')
        return self.parse(file_path.as_posix(), raw_text)

    def parse(self, file_path: str, raw_text: str) -> LawDocument:
        """解析法律文本"""
        lines = self._clean_lines(raw_text)
        if not lines:
            raise ValueError(f'文件为空: {file_path}')

        # 提取标题（第一个非空行）
        title = lines[0] if lines else '未知'

        # 找到目录结束 & 正文开始位置
        body_start = self._find_body_start(lines)

        # 收集序言（标题之后、正文开始之前）
        preamble_lines = lines[1:body_start]
        preamble = '\n'.join(line for line in preamble_lines if line.strip())

        doc = LawDocument(
            file_path=file_path,
            title=title,
            preamble=preamble,
        )

        # 逐行解析正文
        body_lines = lines[body_start:]
        self._parse_body(body_lines, doc)

        # 构建所有条文的扁平列表
        self._flatten_articles(doc)

        return doc

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_lines(text: str) -> list[str]:
        """清洗文本行：去 BOM、合并空白、去空行（保留有意义空行）"""
        text = text.strip('\ufeff').strip()
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    @staticmethod
    def _find_body_start(lines: list[str]) -> int:
        """找到正文开始位置（跳过目录区域）

        策略: 目录中只有编/章/节标题，正文才有实际条文。
        遇到目录后，跳过所有标题行，直到遇到非标题内容，
        然后回退到上一个章节标题作为正文开始位置。
        """
        in_toc = False
        last_toc_heading = None
        for i, line in enumerate(lines):
            if _RE_TOC.match(line):
                in_toc = True
                last_toc_heading = i
                continue
            if in_toc:
                if _RE_PART.match(line) or _RE_CHAPTER.match(line):
                    last_toc_heading = i
                    continue
                if _RE_SECTION.match(line):
                    continue
                # 遇到非标题行 → TOC 已结束
                return max(last_toc_heading or i, 1)
        return 1

    def _parse_body(self, lines: list[str], doc: LawDocument) -> None:
        """逐行解析正文，通过标题去重自动处理目录和正文的重复层级"""
        current_part: Optional[Part] = None
        current_chapter: Optional[Chapter] = None
        current_section: Optional[Section] = None
        current_article: Optional[Article] = None
        article_index = 0

        # 标题去重映射：标题 → 已存在的对象
        part_map: dict[str, Part] = {}
        chapter_map: dict[str, Chapter] = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 检查编
            m_part = _RE_PART.match(line)
            if m_part:
                if line in part_map:
                    # 正文中的编已在目录中出现过，复用并激活
                    current_part = part_map[line]
                else:
                    part = Part(title=line)
                    doc.parts.append(part)
                    part_map[line] = part
                    current_part = part
                current_chapter = None
                current_section = None
                current_article = None
                continue

            # 检查章
            m_chapter = _RE_CHAPTER.match(line)
            if m_chapter:
                # 构建唯一键：编标题 + 章标题（同一编下可能同名章）
                chapter_key = (current_part.title if current_part else '') + line
                if chapter_key in chapter_map:
                    current_chapter = chapter_map[chapter_key]
                else:
                    chapter = Chapter(title=line)
                    if current_part is not None:
                        current_part.chapters.append(chapter)
                    else:
                        doc.chapters.append(chapter)
                    chapter_map[chapter_key] = chapter
                    current_chapter = chapter
                current_section = None
                current_article = None
                continue

            # 检查节
            m_section = _RE_SECTION.match(line)
            if m_section:
                if current_chapter is not None:
                    # 检查当前章下是否已有同名节（目录中可能已创建过）
                    existing_sec = None
                    for sec in current_chapter.sections:
                        if sec.title == line:
                            existing_sec = sec
                            break
                    if existing_sec:
                        current_section = existing_sec
                    else:
                        section = Section(title=line)
                        current_chapter.sections.append(section)
                        current_section = section
                else:
                    current_section = Section(title=line)
                current_article = None
                continue

            # 检查条
            m_article = _RE_ARTICLE.match(line)
            if m_article:
                cn_num = m_article.group(1)
                text = m_article.group(2)
                if len(text) >= self.min_article_chars:
                    article_index += 1
                    article = Article(
                        index=article_index,
                        number=cn_num,
                        number_int=_cn_to_int(cn_num),
                        text=text,
                        content=text,
                    )
                    # 挂载到正确的层级
                    if current_section is not None:
                        current_section.articles.append(article)
                    elif current_chapter is not None:
                        current_chapter.articles.append(article)
                    else:
                        doc.articles.append(article)
                    current_article = article
                    continue

            # 非标记行：可能是当前条文的续行
            if current_article is not None and len(line) > 3:
                current_article.content += line
                current_article.text += line

    def _flatten_articles(self, doc: LawDocument) -> None:
        """将所有条文展平到 doc.articles，同时建立上下文引用"""
        # 保留无层级结构的直接条文（如宪法修正案、国务院组织法等没有章/编的法律）
        direct_articles = list(doc.articles)
        doc.articles = []

        def _walk_chapter(chapter: Chapter, part_title: str = '', ch_title: str = ''):
            if chapter.sections:
                for sec in chapter.sections:
                    for art in sec.articles:
                        doc.articles.append(art)
            if chapter.articles:
                for art in chapter.articles:
                    doc.articles.append(art)

        if doc.parts:
            for part in doc.parts:
                for ch in part.chapters:
                    _walk_chapter(ch, part.title, ch.title)
        elif doc.chapters:
            for ch in doc.chapters:
                _walk_chapter(ch, '', ch.title)

        # 合并没有层级结构的直接条文
        if direct_articles:
            doc.articles.extend(direct_articles)

        # 确保 index 连续
        for i, art in enumerate(doc.articles, 1):
            art.index = i


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def build_all_documents(data_dir: str | Path) -> list[LawDocument]:
    """解析 LawData 目录下所有 .txt 文件"""
    data_dir = Path(data_dir)
    parser = LawParser()
    docs = []
    # 递归扫描（支持 LawData 下的子目录，如 laws/、regulations/ 等增量爬取结果）
    for fp in sorted(data_dir.rglob('*.txt')):
        try:
            doc = parser.parse_file(fp)
            docs.append(doc)
            logger.info(f'解析完成: {doc.title}  (共 {len(doc.articles)} 条)')
        except Exception as e:
            logger.error(f'解析失败 {fp.name}: {e}')
    return docs


def print_hierarchy(doc: LawDocument) -> None:
    """调试用：打印法律文档的结构层级"""
    print(f'\n{"="*60}')
    print(f'法律: {doc.title}')
    print(f'条文数: {len(doc.articles)}')
    print(f'序言: {doc.preamble[:80]}...' if len(doc.preamble) > 80 else f'序言: {doc.preamble}')

    def _print_chapter(ch: Chapter, indent: int = 2):
        prefix = '  ' * indent
        print(f'{prefix}章: {ch.title}')
        if ch.sections:
            for sec in ch.sections:
                print(f'{prefix}  节: {sec.title}  ({len(sec.articles)} 条)')
                for art in sec.articles[:2]:
                    print(f'{prefix}    第{art.number}条: {art.text[:60]}...')
        elif ch.articles:
            print(f'{prefix}  ({len(ch.articles)} 条)')
            for art in ch.articles[:3]:
                print(f'{prefix}  第{art.number}条: {art.text[:60]}...')

    if doc.parts:
        for part in doc.parts:
            print(f'  编: {part.title}')
            for ch in part.chapters:
                _print_chapter(ch, 3)
    elif doc.chapters:
        for ch in doc.chapters[:5]:
            _print_chapter(ch)
        if len(doc.chapters) > 5:
            print(f'  ... 共 {len(doc.chapters)} 章')
