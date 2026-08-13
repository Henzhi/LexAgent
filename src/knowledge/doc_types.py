"""法律法规分类规范层（单一权威来源）。

统一全系统的 doc_type 分类，对齐「国家法律法规数据库」(flk.npc.gov.cn)
的顶级检索分类。此前后端、爬虫、前端各自维护一套 doc_type，导致
同物两名（司法解释=judicial/interpretation）、前端缺类型等问题。

规范 doc_type（flk 顶级分类）：
    constitution        宪法
    law                 法律
    regulation          行政法规
    supervision         监察法规
    local_regulation    地方性法规
    judicial_interpretation  司法解释
    case                典型案例（非 flk 数据源，前端手工上传）

说明：flk 将"法律"进一步细分为 7 个部门法（码 110/120/130/140/150/160/180），
但具体码→部门法的精确映射未获官方权威来源确认，故本层仍以顶级分类为
存储单位，不冒进拆分；部门法细分留作后续增强。
"""
from __future__ import annotations

# flk 分类码 -> 规范 doc_type（爬虫用）
# 分类码来自 2026 实测：宪法 100；法律 110/120/130/140/150/160/180；
# 行政法规 210；监察法规 220；地方法规 230；司法解释 320/340
FLK_CODE_TO_DOC_TYPE: dict[int, str] = {
    100: "constitution",
    110: "law", 120: "law", 130: "law", 140: "law",
    150: "law", 160: "law", 180: "law",
    210: "regulation",
    220: "supervision",
    230: "local_regulation",
    320: "judicial_interpretation",
    340: "judicial_interpretation",
}

# 规范 doc_type 元信息（中文名 / 前端标签 / 是否可爬虫采集）
DOC_TYPE_INFO: dict[str, dict] = {
    "constitution": {"label": "宪法", "crawlable": True},
    "law": {"label": "法律", "crawlable": True},
    "regulation": {"label": "行政法规", "crawlable": True},
    "supervision": {"label": "监察法规", "crawlable": True},
    "local_regulation": {"label": "地方性法规", "crawlable": True},
    "judicial_interpretation": {"label": "司法解释", "crawlable": True},
    "case": {"label": "典型案例", "crawlable": False},  # flk 数据源不提供
}

# 历史数据旧值 -> 规范 doc_type（兼容迁移前的入库数据）
# - judicial / interpretation -> judicial_interpretation（司法解释，同物两名）
# - local -> local_regulation（地方法规，此前爬虫用 local、前端叫 regulation）
LEGACY_ALIASES: dict[str, str] = {
    "judicial": "judicial_interpretation",
    "interpretation": "judicial_interpretation",
    "local": "local_regulation",
}

# flk 列表返回的 flxz（法律形式）-> 规范 doc_type
# 用于爬虫自动分类：不指定 doc_type 时，按每条记录的 flxz 判定归属
FLXZ_TO_DOC_TYPE: dict[str, str] = {
    "宪法": "constitution",
    "法律": "law",
    "行政法规": "regulation",
    "监察法规": "supervision",
    "地方性法规": "local_regulation",
    "司法解释": "judicial_interpretation",
    "自治条例和单行条例": "local_regulation",
    "经济特区法规": "local_regulation",
}


def doc_type_from_flxz(flxz: str | None) -> str | None:
    """根据 flk 返回的 flxz（法律形式）自动判定规范 doc_type。

    无法识别时返回 None（调用方决定兜底策略）。
    """
    if not flxz:
        return None
    key = str(flxz).strip()
    # 先精确匹配
    if key in FLXZ_TO_DOC_TYPE:
        return FLXZ_TO_DOC_TYPE[key]
    # 模糊包含匹配（flk 返回可能带空格/修饰词）
    for name, doc_type in FLXZ_TO_DOC_TYPE.items():
        if name in key:
            return doc_type
    return None


def normalize_doc_type(doc_type: str | None) -> str:
    """将任意输入 doc_type 归一到规范值（未知/None 原样返回）。

    兼容历史数据的旧值（judicial/interpretation/local），
    以及前端上传时可能传入的别名。
    """
    if not doc_type:
        return doc_type or ""
    t = doc_type.strip().lower()
    return LEGACY_ALIASES.get(t, t)


def is_valid_doc_type(doc_type: str | None) -> bool:
    """是否为规范 doc_type 或可归一化的旧值"""
    t = normalize_doc_type(doc_type)
    return t in DOC_TYPE_INFO


def doc_type_label(doc_type: str | None) -> str:
    """返回 doc_type 的中文名（未知返回原值）"""
    t = normalize_doc_type(doc_type)
    info = DOC_TYPE_INFO.get(t)
    return info["label"] if info else (doc_type or "")


def crawlable_types() -> dict[str, str]:
    """flk 数据源可采集的规范类型（doc_type -> 中文名）"""
    return {dt: info["label"] for dt, info in DOC_TYPE_INFO.items() if info["crawlable"]}


# ---------------------------------------------------------------------------
# 法律效力状态（时效性）规范
# ---------------------------------------------------------------------------
# flk 二期 API 实测（2026-05）sxx 取值：
#   1=已废止, 2=已修改, 3=现行有效, 4=尚未生效
# 映射到 documents.status 存储值 + 中文名。效力状态独立于 doc_type，
# 用于识别"有效/废止/未生效"法律。
SXX_TO_STATUS: dict[str, str] = {
    "1": "repealed",    # 已废止
    "2": "revised",     # 已修改（存在新版本）
    "3": "active",      # 现行有效
    "4": "pending",     # 尚未生效（已公布未生效）
}

# 文档状态 -> 中文名 / 前端标签
DOC_STATUS_INFO: dict[str, dict] = {
    "active": {"label": "现行有效", "class": "active"},
    "repealed": {"label": "已废止", "class": "repealed"},
    "revised": {"label": "已修改", "class": "revised"},
    "pending": {"label": "尚未生效", "class": "pending"},
}


def status_from_sxx(sxx: str | int | None) -> str:
    """将 flk 的 sxx 效力状态码映射为规范 status（未知/空返回 active 兜底）。"""
    if sxx is None:
        return "active"
    return SXX_TO_STATUS.get(str(sxx), "active")


def status_label(status: str | None) -> str:
    """返回文档状态的中文名（未知返回原值）"""
    info = DOC_STATUS_INFO.get(status or "")
    return info["label"] if info else (status or "")
