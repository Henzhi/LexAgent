"""法律数据源爬虫。

目前支持:
  - npc: 全国人大官方「国家法律法规数据库」 https://flk.npc.gov.cn

合规提示:
  - 仅用于个人学习 / 研究用途；
  - 请合理控制请求频率（默认每次请求间隔 1s）；
  - 遵守目标网站的 robots.txt 与版权声明。
"""
from __future__ import annotations

from .npc_crawler import (
    NpcLawCrawler,
    CrawlResult,
    TYPE_MAP,
)

__all__ = ["NpcLawCrawler", "CrawlResult", "TYPE_MAP"]
