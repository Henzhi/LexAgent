"""文档解析管道 — 文本清洗器。

处理 PDF/DOCX 解析后的文本噪声：
  - 合并被换行打断的句子
  - 清理页码/页眉/水印残留
  - 统一空白字符
  - 去除控制字符
"""
from __future__ import annotations

import re
from typing import List


class TextCleaner:
    """法律文本清洗器

    用法:
        cleaner = TextCleaner()
        cleaned = cleaner.clean("第一条  为了惩罚  犯罪...")
    """

    # 页码模式
    _PAGE_NUM = re.compile(r'^\s*-?\s*\d{1,4}\s*-?\s*$')
    # 页眉水印（常见法律文件的页眉/重复标记）
    _HEADER_FOOTER = re.compile(
        r'^(中华人民共和国|全国人民代表大会|最高人民法院|第[一二三四五六七八九十百]+[章节]|'
        r'条文注释|法律条文)$'
    )
    # 多余空白
    _MULTI_SPACE = re.compile(r'[ \t]{2,}')
    _MULTI_NEWLINE = re.compile(r'\n{3,}')
    # 换行打断的中文句子（中文句子中间被换行打断）
    _SPLIT_SENTENCE = re.compile(r'([。！？；])\s*\n\s*')

    def clean(self, text: str) -> str:
        """完整清洗流程"""
        if not text or not text.strip():
            return ""

        lines = text.split('\n')
        cleaned_lines = []

        for line in lines:
            stripped = line.strip()
            # 过滤空行、页码、页眉
            if not stripped:
                continue
            if self._PAGE_NUM.match(stripped):
                continue
            if self._HEADER_FOOTER.match(stripped):
                continue
            cleaned_lines.append(stripped)

        text = '\n'.join(cleaned_lines)

        # 合并多余空白
        text = self._MULTI_SPACE.sub(' ', text)
        # 合并多余空行（最多保留 1 个空行）
        text = self._MULTI_NEWLINE.sub('\n\n', text)
        # 中文句子断行合并
        text = self._SPLIT_SENTENCE.sub(r'\1\n', text)

        return text.strip()

    def clean_batch(self, texts: List[str]) -> List[str]:
        """批量清洗"""
        return [self.clean(t) for t in texts]
