"""
文档解析管道单元测试。

验证:
  1. 各解析器可导入
  2. TextCleaner 清洗逻辑
  3. IngestionPipeline 分块逻辑
  4. 文件类型校验
"""
from __future__ import annotations


class TestImports:
    def test_import_pdf_parser(self):
        from src.knowledge.ingestion.pdf_parser import PDFParser
        assert PDFParser is not None

    def test_import_docx_parser(self):
        from src.knowledge.ingestion.docx_parser import DocxParser
        assert DocxParser is not None

    def test_import_cleaner(self):
        from src.knowledge.ingestion.text_cleaner import TextCleaner
        assert TextCleaner is not None

    def test_import_pipeline(self):
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        assert IngestionPipeline is not None

    def test_import_task_status(self):
        from src.knowledge.ingestion.pipeline import TaskStatus
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.DONE == "done"


class TestTextCleaner:
    def test_clean_normal_text(self):
        from src.knowledge.ingestion.text_cleaner import TextCleaner
        cleaner = TextCleaner()
        text = "第一条  为了惩罚  犯罪，保护人民。\n\n第二条  根据宪法，结合..."
        result = cleaner.clean(text)
        assert "第一条" in result
        assert "第二条" in result
        # 多余空格应被压缩
        assert "为了惩罚 犯罪" not in result or "为了惩罚" in result

    def test_clean_page_numbers(self):
        from src.knowledge.ingestion.text_cleaner import TextCleaner
        cleaner = TextCleaner()
        # 页码行应该被过滤
        text = "第一条 内容\n123\n第二条 内容\n- 45 -"
        result = cleaner.clean(text)
        assert "123" not in result.split("\n")
        assert "- 45 -" not in result.split("\n")

    def test_clean_empty(self):
        from src.knowledge.ingestion.text_cleaner import TextCleaner
        cleaner = TextCleaner()
        assert cleaner.clean("") == ""
        assert cleaner.clean("   ") == ""

    def test_clean_batch(self):
        from src.knowledge.ingestion.text_cleaner import TextCleaner
        cleaner = TextCleaner()
        results = cleaner.clean_batch(["第一条 内容", "", "第二条 内容"])
        assert len(results) == 3
        assert results[1] == ""


class TestPipelineSplit:
    def test_split_short_paragraphs(self):
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "第一条 为了惩罚犯罪。\n\n第二条 结合我国实际情况。"
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "law")
        assert len(chunks) == 2
        assert all(c["chunk_type"] == "article" for c in chunks)

    def test_split_long_paragraph(self):
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        # 构造超长段落（大于500字）
        long_text = "。".join(["第X条规定" * 10 for _ in range(20)]) + "。"
        chunks = IngestionPipeline._split_paragraphs(long_text, "doc_id", "law", max_chars=100)
        # 超长段落应该被拆分
        assert len(chunks) > 1

    def test_split_empty_text(self):
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        chunks = IngestionPipeline._split_paragraphs("", "doc_id", "law")
        assert chunks == []

    def test_split_articles_without_blank_lines(self):
        """回归：条文间无空行时不得糅合成一个 chunk（原按 \\n\\n 段落切分）"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "第一条 为了惩罚犯罪。第二条 刑罚的种类。第三条 本法自公布之日起施行。"
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "law")
        assert len(chunks) == 3
        assert all(c["chunk_type"] == "article" for c in chunks)
        assert "第一条" in chunks[0]["content"]
        assert "第三条" in chunks[2]["content"]

    def test_split_preamble_and_articles(self):
        """序言段独立成块，条文按边界切分"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "为了惩罚犯罪，保护人民，制定本法。第一条 犯罪必须依法处罚。第二条 刑罚由法院决定。"
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "law")
        assert len(chunks) == 3
        assert "为了惩罚犯罪" in chunks[0]["content"]
        assert "第一条" in chunks[1]["content"]

    def test_split_long_article_keeps_prefix(self):
        """超长条文拆成多块时，续块保留条号前缀以便引用追溯"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "第一条 " + "应当依法追究刑事责任。" * 30  # 超 500 字
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "law", max_chars=100)
        assert len(chunks) > 1
        assert all("第一条" in c["content"] for c in chunks)

    def test_split_case_by_paragraphs(self):
        """回归：案例为叙事文，按自然段切分，不做条文/句子硬拆分"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = ("某公司未依法缴纳社会保险费。\n\n"
                "法院经审理认为，该公司行为违法。\n\n"
                "判决如下：补缴并支付赔偿金。")
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "case")
        assert len(chunks) == 3
        assert all(c["chunk_type"] == "case" for c in chunks)
        assert "缴纳社会保险费" in chunks[0]["content"]

    def test_split_case_long_paragraph(self):
        """案例超长段落保底按句号拆分，但类型仍为 case"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "案情简述。" * 30 + "\n\n判决结果。"  # 第一段超长
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "case", max_chars=100)
        assert len(chunks) >= 2
        assert all(c["chunk_type"] == "case" for c in chunks)

    def test_article_doc_types_split_by_articles(self):
        """法律/行政法规/宪法/监察法规按条文切分（chunk_type=article）"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "第一条 甲。第二条 乙。"
        for dt in ("law", "regulation", "constitution", "supervision"):
            chunks = IngestionPipeline._split_paragraphs(text, "doc_id", dt)
            assert len(chunks) == 2, dt
            assert all(c["chunk_type"] == "article" for c in chunks)

    def test_interpretation_split_by_paragraphs(self):
        """司法解释按自然段切分（无条号结构，不做条文硬拆分）"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "本解释自发布之日起施行。\n\n此前发布的司法解释与本解释不一致的，以本解释为准。"
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "interpretation")
        assert len(chunks) == 2
        assert all(c["chunk_type"] == "interpretation" for c in chunks)

    def test_judicial_interpretation_split_by_paragraphs(self):
        """规范 doc_type=judicial_interpretation 同样按自然段切分"""
        from src.knowledge.ingestion.pipeline import IngestionPipeline
        text = "一、本解释适用于……\n\n二、此前解释与本解释不一致的，以本解释为准。"
        chunks = IngestionPipeline._split_paragraphs(text, "doc_id", "judicial_interpretation")
        assert len(chunks) == 2
        assert all(c["chunk_type"] == "judicial_interpretation" for c in chunks)

    def test_fulltext_mode(self):
        """全文模式：整篇一个 chunk，直接全文召回"""
        from src.knowledge.ingestion import pipeline as p
        p.set_fulltext_doc_types(["interpretation", "case"])
        try:
            text = "案例全文第一段。\n\n案例全文第二段。\n\n案例全文第三段。"
            chunks = p.IngestionPipeline._split_paragraphs(text, "doc_id", "case")
            assert len(chunks) == 1
            assert chunks[0]["chunk_type"] == "case"
            assert "案例全文第三段" in chunks[0]["content"]
            # 超长整篇保底拆分
            long_text = "长文。" * 100
            chunks2 = p.IngestionPipeline._split_paragraphs(long_text, "doc_id", "case", max_chars=100)
            assert len(chunks2) > 1
        finally:
            p.set_fulltext_doc_types([])


class TestValidation:
    def test_allowed_extensions(self):
        from src.knowledge.ingestion.pipeline import ALLOWED_EXTENSIONS
        assert ".pdf" in ALLOWED_EXTENSIONS
        assert ".docx" in ALLOWED_EXTENSIONS
        assert ".txt" in ALLOWED_EXTENSIONS
        assert ".exe" not in ALLOWED_EXTENSIONS
