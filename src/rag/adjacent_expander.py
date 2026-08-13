"""
连续片段检索扩展器。

在向量检索结果的基础上，自动拉取相邻条文。
例如检索到第43条时，自动补入第42条和第44条（如果存在）。

实现方式：
    - 构建时将 LawDocument 的每条条文保存为 article_map.json
    - 检索后通过 (law_name, article_number_int) 做 O(1) 查找相邻条文
    - 相邻条文以装饰器模式包装基础检索器
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .retriever import BaseRetriever, RetrievedDoc

logger = logging.getLogger(__name__)


class AdjacentExpander(BaseRetriever):
    """检索后自动扩展相邻条文的装饰器"""

    def __init__(
        self,
        base_retriever: BaseRetriever,
        article_map_path: str | Path,
        window: int = 2,
    ):
        """
        Args:
            base_retriever: 基础检索器 (pgvector / Reranker 装饰链)
            article_map_path: article_map.json 文件路径
            window: 相邻窗口大小，±window 条
        """
        self._base = base_retriever
        self._window = window
        self._map: dict[str, dict[str, dict]] = self._load_map(article_map_path)
        logger.info(f"相邻扩展就绪: window={window}, 涵盖 {len(self._map)} 部法律")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 5, **kwargs) -> list[RetrievedDoc]:
        results = self._base.search(query, top_k=top_k, **kwargs)
        return self._expand(results)

    def is_ready(self) -> bool:
        return self._base.is_ready()

    # ------------------------------------------------------------------
    # 核心逻辑
    # ------------------------------------------------------------------

    def _expand(self, results: list[RetrievedDoc]) -> list[RetrievedDoc]:
        """对每条检索结果扩展相邻条文"""
        seen: set[tuple[str, str]] = set()
        expanded: list[RetrievedDoc] = []

        # 1. 原始结果优先（按原始分数排序）
        for r in results:
            key = (r.law_name, r.article_range)
            if key not in seen:
                seen.add(key)
                expanded.append(r)

        # 2. 对每条结果扩展 ±window 相邻条文
        for r in results:
            law_name = r.law_name
            if law_name not in self._map:
                continue

            article_nums = self._parse_range_bounds(r.article_range)
            for num in article_nums:
                for offset in range(-self._window, self._window + 1):
                    if offset == 0:
                        continue
                    adj_num = num + offset
                    if adj_num < 1:
                        continue

                    adj_key = str(adj_num)
                    article_data = self._map[law_name].get(adj_key)
                    if article_data is None:
                        continue

                    art_range = article_data.get("article_range", "")
                    seen_key = (law_name, art_range)
                    if seen_key not in seen:
                        seen.add(seen_key)
                        expanded.append(RetrievedDoc(
                            content=article_data.get("content", ""),
                            score=0.0,  # 相邻条文不打分
                            law_name=law_name,
                            chapter=article_data.get("chapter", ""),
                            section=article_data.get("section", ""),
                            article_range=art_range,
                            chunk_type="adjacent",
                        ))

        return expanded

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_range_bounds(article_range: str) -> list[int]:
        """解析条文范围，返回边界数字列表。

        "第一条" → [1]
        "第一条至第三条" → [1, 3]
        "第十条至第十二条" → [10, 12]
        """
        import re

        # 中文数字 → 整数映射
        cn_map = {
            '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
            '百': 100, '千': 1000,
        }

        def _cn2int(cn: str) -> int:
            result = 0
            unit = 1
            i = len(cn) - 1
            while i >= 0:
                val = cn_map.get(cn[i], 0)
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

        # 匹配 "第X条" 或 "第X条至第Y条"
        nums = re.findall(r'第([一二三四五六七八九十百千]+)条', article_range)
        return [_cn2int(n) for n in nums]

    @staticmethod
    def _load_map(path: str | Path) -> dict:
        """加载条文映射文件"""
        p = Path(path)
        if not p.exists():
            logger.warning(f"条文映射文件不存在: {p}，相邻扩展将不生效")
            return {}
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data

    @staticmethod
    def build_article_map(all_docs: list) -> dict:
        """从 LawDocument 列表构建条文映射。

        Args:
            all_docs: LawParser 解析后的 LawDocument 列表

        Returns:
            {law_name: {article_number_int: {content, article_range, chapter, section}}}
        """
        m: dict[str, dict[str, dict]] = {}

        for doc in all_docs:
            law_name = doc.title
            m[law_name] = {}

            # 收集每条条文的层次信息
            article_context: dict[int, dict] = {}  # article_index → {chapter, section}

            if doc.parts:
                for part in doc.parts:
                    for ch in part.chapters:
                        _collect_article_ctx(ch, law_name, article_context)
            elif doc.chapters:
                for ch in doc.chapters:
                    _collect_article_ctx(ch, law_name, article_context)

            for art in doc.articles:
                ctx = article_context.get(art.index, {})
                m[law_name][str(art.number_int)] = {
                    "content": art.content,
                    "article_range": f"第{art.number}条",
                    "chapter": ctx.get("chapter", ""),
                    "section": ctx.get("section", ""),
                }

        return m

    @staticmethod
    def save_article_map(article_map: dict, path: str | Path) -> None:
        """保存条文映射为 JSON 文件"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(article_map, f, ensure_ascii=False, indent=2)
        logger.info(f"条文映射已保存: {p} ({len(article_map)} 部法律)")


def _collect_article_ctx(chapter, law_name: str, ctx: dict) -> None:
    """收集每个条文的章/节上下文"""
    ch_title = chapter.title
    if chapter.sections:
        for sec in chapter.sections:
            for art in sec.articles:
                ctx[art.index] = {"chapter": ch_title, "section": sec.title}
    for art in chapter.articles:
        ctx[art.index] = {"chapter": ch_title, "section": ""}
