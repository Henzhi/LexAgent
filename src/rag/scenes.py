"""
场景分类模块（M3 / F11）。

将用户查询映射到 PRD §5.2 定义的业务场景，判定 A 类（全自动）或 B 类（需人工确认），
为 F12 人工确认节点提供判据（REQ-E1：先意图识别与场景分类，再进入 Agent 编排）。

设计要点（对应事项 rpuMIf 的结论）：
- **场景清单是数据、分类逻辑是代码**：产品后续调整场景清单（增删场景、改 A/B 归属、
  改关键词）只需改 `SCENES` 列表，不动任何函数。原本的「需产品定清单」因此不再阻塞开发。
- v0 场景清单直接取自 PRD §5.2 的 10 个典型场景。
- **未匹配时保守回落**：全部场景得分为 0 时返回默认 A 类场景 `legal_qa`，
  绝不因分类失败阻断回答（REQ-UW）。

匹配规则：加权关键词打分，取最高分。
    score = 正则命中数 × 3.0 + 强特征词命中数 × 2.0 + 普通关键词命中数 × 1.0

分三级是因为中文查询里「合同」这类通用词会同时命中多个场景，
需要让「起草」「审查」这类强特征词和「第X条」这类正则压过通用词。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 打分权重：正则 > 强特征词 > 普通关键词
_WEIGHT_PATTERN = 3.0
_WEIGHT_STRONG = 2.0
_WEIGHT_NORMAL = 1.0

# 场景类型
KIND_A = "A"  # 全自动
KIND_B = "B"  # 需人工确认（F12）


@dataclass(frozen=True)
class Scene:
    """一个业务场景的配置项（PRD §5.2 的一行）。"""

    id: str  # 场景标识，如 "contract_draft"
    name: str  # 场景名称，如 "合同起草"
    kind: str  # KIND_A / KIND_B
    tools: tuple[str, ...] = ()  # 该场景建议使用的工具（供后续工具白名单用）
    keywords: tuple[str, ...] = ()  # 普通关键词（权重 1.0）
    strong_keywords: tuple[str, ...] = ()  # 强特征词（权重 2.0）
    patterns: tuple[str, ...] = ()  # 正则（权重 3.0）
    # 预编译后的正则，构建时由 __post_init__ 填充（frozen dataclass 用 object.__setattr__）
    _compiled: tuple = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        compiled = tuple(re.compile(p) for p in self.patterns)
        object.__setattr__(self, "_compiled", compiled)


@dataclass(frozen=True)
class SceneMatch:
    """场景分类结果。"""

    scene_id: str  # 命中的场景 id（未命中为默认场景 id）
    name: str  # 场景名称
    kind: str  # KIND_A / KIND_B
    tools: tuple[str, ...]  # 该场景的工具白名单
    score: float  # 匹配得分（0 表示未命中任何场景）
    matched: bool  # 是否命中（False 表示走了默认回落）

    def needs_confirmation(self) -> bool:
        """是否需要人工确认（B 类场景）。"""
        return self.kind == KIND_B


# ---------------------------------------------------------------------------
# 场景清单（数据层 —— 产品直接改这里）
# ---------------------------------------------------------------------------

SCENES: tuple[Scene, ...] = (
    # ---- A 类：全自动 ----
    Scene(
        id="legal_lookup",
        name="法律检索",
        kind=KIND_A,
        tools=("retrieve_knowledge", "web_search", "legal_source_search"),
        keywords=("法条", "条文", "法规", "法律", "规定"),
        strong_keywords=(
            "是什么", "如何规定", "怎么规定", "怎么规定的", "规定是什么",
            "查一下", "查询", "检索",
        ),
        patterns=(r"第[一二三四五六七八九十百千万零\d]+条",),
    ),
    Scene(
        id="legal_qa",
        name="法律咨询问答",
        kind=KIND_A,
        tools=("retrieve_knowledge", "memory"),
        keywords=(
            "违法吗", "合法吗", "可以吗", "能吗", "怎么办", "怎么处理", "是否",
            "需要赔偿", "赔偿吗", "要不要赔", "能不能",
        ),
        strong_keywords=("咨询", "问一下", "请问", "我想问", "要不要", "有没有权利"),
    ),
    Scene(
        id="regulation_tracking",
        name="法规动态追踪",
        kind=KIND_A,
        tools=("web_search", "legal_source_search"),
        keywords=("新规", "修订", "修改", "最新", "动态", "施行", "发布"),
        strong_keywords=("最新动态", "有没有新", "最近出台", "政策变化", "新修订", "生效了吗"),
        patterns=(r"20\d{2}\s*年.{0,6}(新规|修订|出台|施行)",),
    ),
    Scene(
        id="case_analysis",
        name="案例分析",
        kind=KIND_A,
        tools=("retrieve_knowledge", "web_search", "legal_source_search"),
        keywords=("案例", "判例", "判决", "怎么判", "裁判"),
        strong_keywords=("分析一下", "怎么看", "评析", "解读"),
    ),

    # ---- B 类：需人工确认 ----
    Scene(
        id="similar_case_report",
        name="类案检索报告",
        kind=KIND_B,
        tools=("retrieve_knowledge", "web_search", "legal_source_search"),
        keywords=("类案", "相似案例", "类似案例", "同类案件", "报告"),
        strong_keywords=(
            "类案检索", "检索报告", "类案报告", "相似判决",
            "类似的", "类似的判决", "出报告", "成报告", "汇总成",
        ),
    ),
    Scene(
        id="contract_draft",
        name="合同起草",
        kind=KIND_B,
        tools=("retrieve_knowledge", "web_search"),
        keywords=("合同", "协议"),
        strong_keywords=("起草", "拟订", "拟定", "写一份", "帮我写", "拟一份", "起草一份"),
    ),
    Scene(
        id="contract_review",
        name="合同审查",
        kind=KIND_B,
        tools=("retrieve_knowledge", "web_search", "memory"),
        keywords=("合同", "协议", "条款"),
        strong_keywords=("审查", "审核", "把一下关", "修改意见", "有没有问题", "风险点", "看看这份"),
    ),
    Scene(
        id="legal_document",
        name="法律文书生成",
        kind=KIND_B,
        tools=("retrieve_knowledge", "legal_source_search"),
        keywords=("起诉状", "答辩状", "上诉状", "申请书", "文书"),
        strong_keywords=("写一份", "起草", "帮我写", "怎么写"),
        patterns=(r"(起诉|答辩|上诉|仲裁|执行|保全|申诉)(状|申请书|书)",),
    ),
    Scene(
        id="due_diligence",
        name="尽职调查辅助",
        kind=KIND_B,
        tools=("web_search", "legal_source_search"),
        keywords=("尽职调查", "背调", "风险排查"),
        strong_keywords=("尽调", "尽职调查", "背景调查", "投前"),
    ),
    Scene(
        id="compliance_check",
        name="合规检查 / 证据分类",
        kind=KIND_B,
        tools=("retrieve_knowledge", "web_search"),
        keywords=("证据", "合规", "整改"),
        strong_keywords=("合规检查", "合规审查", "证据分类", "证据清单", "合规体检"),
    ),
)

# 默认回落场景：A 类，未命中任何场景时使用。
# 选「法律咨询问答」是因为它最通用 —— 保守回落不应把用户拖进 B 类的确认流程。
_DEFAULT_SCENE_ID = "legal_qa"
DEFAULT_SCENE: Scene = next(s for s in SCENES if s.id == _DEFAULT_SCENE_ID)

_SCENE_BY_ID: dict[str, Scene] = {s.id: s for s in SCENES}


# ---------------------------------------------------------------------------
# 分类逻辑（代码层）
# ---------------------------------------------------------------------------


def get_scene(scene_id: str) -> Scene | None:
    """按 id 取场景配置，不存在返回 None。"""
    return _SCENE_BY_ID.get(scene_id)


def scene_ids() -> list[str]:
    """返回全部场景 id（供配置校验 / 测试使用）。"""
    return [s.id for s in SCENES]


def _score_scene(scene: Scene, query: str) -> float:
    """计算单个场景对查询的匹配得分。"""
    score = 0.0
    for kw in scene.keywords:
        if kw in query:
            score += _WEIGHT_NORMAL
    for kw in scene.strong_keywords:
        if kw in query:
            score += _WEIGHT_STRONG
    for pattern in scene._compiled:
        if pattern.search(query):
            score += _WEIGHT_PATTERN
    return score


def classify_scene(query: str) -> SceneMatch:
    """将查询分类到业务场景。

    未命中任何场景（全部得分为 0）时返回默认 A 类场景 `legal_qa`，
    `matched=False` —— 保守回落，不因分类失败阻断回答（REQ-UW）。

    Args:
        query: 用户原始查询（调用方需保证已过滤闲聊，本函数不判 casual）

    Returns:
        SceneMatch：含 scene_id / name / kind / tools / score / matched
    """
    q = (query or "").strip()
    if not q:
        return SceneMatch(
            scene_id=DEFAULT_SCENE.id,
            name=DEFAULT_SCENE.name,
            kind=DEFAULT_SCENE.kind,
            tools=DEFAULT_SCENE.tools,
            score=0.0,
            matched=False,
        )

    best: Scene | None = None
    best_score = 0.0
    for scene in SCENES:
        score = _score_scene(scene, q)
        if score > best_score:
            best_score = score
            best = scene

    if best is None or best_score <= 0:
        logger.debug("场景分类未命中，回落默认场景: %s（query=%r）", DEFAULT_SCENE.id, q[:50])
        return SceneMatch(
            scene_id=DEFAULT_SCENE.id,
            name=DEFAULT_SCENE.name,
            kind=DEFAULT_SCENE.kind,
            tools=DEFAULT_SCENE.tools,
            score=0.0,
            matched=False,
        )

    return SceneMatch(
        scene_id=best.id,
        name=best.name,
        kind=best.kind,
        tools=best.tools,
        score=best_score,
        matched=True,
    )
