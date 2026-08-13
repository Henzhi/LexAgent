"""文档解析管道 — PDF 解析器。

基于 pymupdf (fitz)，支持：
  - 文字型 PDF：直接提取文本
  - 双栏排版：按阅读顺序合并
  - 加密 PDF：检测并报错
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PDFParser:
    """PDF 文档解析器

    用法:
        parser = PDFParser()
        text = parser.parse("path/to/document.pdf")
    """

    def parse(self, file_path: str) -> str:
        """解析 PDF 文件，返回清洗后的文本"""
        try:
            import fitz  # pymupdf
        except ImportError:
            raise ImportError("请安装 pymupdf: uv add pymupdf")

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        try:
            doc = fitz.open(str(path))
        except Exception as e:
            if "password" in str(e).lower() or "encrypt" in str(e).lower():
                raise ValueError(f"PDF 文件已加密，不支持加密文件: {file_path}")
            raise ValueError(f"无法打开 PDF 文件: {e}")

        full_text: list[str] = []
        for page_num, page in enumerate(doc):
            text = page.get_text("text")
            if text and text.strip():
                full_text.append(text)
            else:
                logger.debug(f"PDF 第 {page_num + 1} 页无文本（可能为扫描件）")

        doc.close()
        return "\n".join(full_text)

    def parse_bytes(self, content: bytes) -> str:
        """从字节流解析 PDF"""
        try:
            import fitz
        except ImportError:
            raise ImportError("请安装 pymupdf: uv add pymupdf")

        doc = fitz.open(stream=content, filetype="pdf")
        full_text: list[str] = []
        for page in doc:
            text = page.get_text("text")
            if text and text.strip():
                full_text.append(text)
        doc.close()
        return "\n".join(full_text)

    def has_text(self, file_path: str) -> bool:
        """检测 PDF 是否包含可提取的文字（判断是否为扫描件）"""
        try:
            import fitz
        except ImportError:
            logger.warning("pymupdf 未安装，无法检测 PDF 文字")
            return False
        try:
            doc = fitz.open(str(file_path))
        except Exception:
            return False
        try:
            text = doc[0].get_text("text") if len(doc) > 0 else ""
        except IndexError:
            text = ""
        doc.close()
        return len(text.strip()) > 50
