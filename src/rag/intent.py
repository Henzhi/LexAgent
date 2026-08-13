"""
统一意图识别模块。

合并原 src/rag/engine.py（正则 + LLM 自省路由）与 src/agents/graph.py（关键词集合）
两份重复实现，提供单一事实来源：

- is_casual_query(query)  -> 是否为明显闲聊/问候（正则判定，供 RAGEngine 路由与元数据标记）
- classify_intent(query)  -> 是否为法律问题（关键词判定，供 LangGraph Agent 路由）
- needs_retrieval(query, llm) -> 是否需要检索（正则 + 长查询快路径 + LLM 自省兜底）
- sanitize_input(query)   -> 输入安全过滤（Prompt 注入防御 + 内容审核）

注意：is_casual_query 与 classify_intent 使用不同算法（历史行为需分别保留，
否则会破坏既有单测），但共用本模块的词典常量，消除之前分散在两处的重复代码。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 词典常量（单一来源）
# ---------------------------------------------------------------------------

# 闲聊类正则模式 — 匹配后直接走 LLM 回复，跳过检索
_CASUAL_PATTERNS = [
    # 问候
    r'^(你好|您好|hi|hello|嗨|早上好|下午好|晚上好|大家好)',
    r'^(在吗|在不|在不在)$',
    # 感谢
    r'^(谢谢|感谢|多谢|thanks|thank)',
    # 告别
    r'^(再见|拜拜|bye|晚安|回头见)',
    # 自我介绍
    r'^(你是谁|你叫什么|你是什么|你的名字|介绍.*自己)',
    r'^(你能做什么|你会什么|你有什么功能|你能干什么)',
    # 纯闲聊
    r'^(今天天气|天气怎么样|讲个笑话|说个笑话)',
    r'^(嗯|哦|好吧|好的|OK|ok)$',
]

# 身份/自我介绍类模式 — 只用于 classify_intent / classify_query_type（Agent 路径）。
# 不进入 _CASUAL_PATTERNS：避免影响 RAGEngine 的 needs_retrieval 快路径
# （"我是农民工"这类短句在非 Agent 路径仍交给 LLM 自省判断，防止误伤真实法律咨询）。
_SELF_INTRO_PATTERNS = [
    # 反身/身份问句（覆盖顺序词插入等变体："你还记得我是谁吗"等）
    r'^(我是谁|我是干什么的|我是做什么的|我是什么人|我是你|我是什么)',
    r'^(你认识我|你知道我是谁|你知道我|你记得我|你还记得我|还记得我|还记得我是谁|还记得我是谁吗)',
    r'^(我叫什么|我叫什么名字|我叫什么名|我姓什么|我姓|我的名字|我的姓名)',
    # 短自我介绍（≤6 字，如"我是痕至""我叫小明""我姓王"）
    r'^(我是|我叫|我姓).{0,4}$',
]

# 闲聊短语 — 精确匹配即跳过检索
_CASUAL_PHRASES = {
    # 问候
    "你好", "您好", "hi", "hello", "嗨", "哈喽",
    "早上好", "下午好", "晚上好", "中午好", "晚安", "早",
    "你是谁", "你叫什么", "你是什么", "你的名字",
    "介绍自己", "自我介绍", "你是啥",
    "在吗", "在不在", "在线吗",
    "你能做什么", "你会什么", "你有什么功能",
    "开始", "开始咨询", "测试", "test", "试试",
    # 感谢
    "谢谢", "感谢", "多谢", "thanks", "thank you",
    "非常感谢", "十分感谢", "万分感谢",
    # 告别
    "再见", "拜拜", "bye", "goodbye", "走了", "告辞",
}

# 能力问句模式 — 命中时返回"系统固定能力清单"而非让 LLM 自由发挥
# （防止 LLM 编造本系统不具备的能力，如写代码/翻译/作图等）。
_CAPABILITY_PATTERNS = [
    r'^(你能做什么|你可以做什么|你能做些什么|你能干什么|你能干哪些事|你能干嘛|你能干啥|你会什么|你会啥|你都会什么|你都会啥|你有什么功能|你有什么能力|你有什么用处|你有什么用途|你有什么用|你擅长什么|你擅长啥|你能帮我什么|你能帮到我什么|你能帮我做什么|你能帮我干什么|你能提供什么帮助|你能处理什么)',
    r'^(你有哪些功能|你有些什么功能|你的功能是什么|你的能力是什么|介绍一下你的功能|介绍一下你能做什么|你是做什么的|你是干嘛的|你是干啥的|你是干什么的)',
    r'^你能解答.*法律|你能查.*法律|你能检索.*法律',
]

# 宽松组合正则（去标点后匹配）：
# 把"主语(你/您，可选) + 情态词(能/会/可以/擅长) + 动作(做/干/帮) + 宾语(什么/啥/嘛) + 语气词(可选)"
# 结构化解耦，避免"错一个字就匹配不上"（如"你会做什么""你会做什么吗""你能干些啥""你都会啥"）。
_CAPABILITY_LOOSE = [
    # 你会做什么 / 你能做些什么 / 你能干些啥 / 你擅长干嘛
    r'^(你|您)?(能|会|可以|擅长)(做|干)(什么|啥|嘛|些什么|干些什么|些啥)(吗|呀|呢|啊|吧)?$',
    # 你会啥 / 你都会什么 / 你都会些啥
    r'^(你|您)?(能|会|可以)?都?会(什么|啥|嘛|些啥)(吗|呀|呢|啊|吧)?$',
    # 你会什么 / 你擅长啥 / 你能啥(口语)
    r'^(你|您)?(能|会|可以|擅长)(什么|啥|嘛)(吗|呀|呢|啊|吧)?$',
    # 你能帮我做什么 / 你可以帮忙干些啥 / 你能帮到我什么
    r'^(你|您)?(能|会|可以|擅长)?(帮|帮忙|帮助|帮到)(我)?(做|干|处理|解决)?(什么|啥|些什么|些啥)(吗|呀|呢|啊|吧)?$',
    # 你有什么功能 / 你有啥用处 / 你有什么用
    r'^(你|您)?有(什么|哪些|啥)(功能|能力|用处|用途|作用|用)(吗|呀|呢|啊|吧)?$',
]

# 能力关键词（宽松兜底：以"你/您"为主语 + 含以下词 + 不含法律关键词 → 视为能力问句）
_CAPABILITY_KEYWORDS = [
    "能做什么", "能做些什么", "能干什么", "能干些什么", "能干嘛", "能干啥", "能帮",
    "会什么", "会啥", "会做啥", "会做什么", "都会什么", "都会啥",
    "有什么功能", "有什么能力", "有什么用处", "有什么用途", "有什么用",
    "擅长什么", "擅长啥",
    "能帮我什么", "能帮到我什么", "能帮我做什么", "能帮忙做什么", "能帮我干",
    "有哪些功能", "什么功能", "功能是什么", "能力是什么",
    "能提供什么", "能处理什么",
]

# 能力问句的固定回复模板（系统能力边界清单）。
# 直接返回而非调 LLM，避免 LLM 编造本系统不具备的能力（写代码/翻译/作图等）。
# 供 routes / nodes / graph 各层 casual 出口统一使用，保证所有路径行为一致。
# 法律数量 {count} 由 get_capability_reply() 运行时从知识库动态获取。
_CAPABILITY_REPLY_TEMPLATE = """我可以帮你解答中国法律法规相关的问题，具体能力包括：

📚 **法律条文查询**
- 查询特定法律条款的具体内容（如"民法典关于违约金的规定"）
- 覆盖 {count} 部法律、行政法规与司法解释：民法典、刑法、劳动法、劳动合同法、治安管理处罚法等

⚖️ **法律咨询**
- 就具体行为/情形判断是否违法、责任归属（如"打架被拘留，最长多久？"）
- 婚姻、合同、劳动、工伤、继承等常见民事纠纷

🔎 **法规细节检索**
- 精确引用法律名称、章节、条款号
- 结合上下文给出针对性解答

我基于中国现行法律法规的公开文本提供参考信息。请注意：**我的回答仅供参考，不构成专业法律意见**；涉及重大权益的具体事务，建议咨询执业律师。"""

# 动态获取法律数量的缓存（60s 内不重复查库）
_capability_count_cache: dict = {"count": None, "ts": 0.0}
_CAPABILITY_COUNT_TTL = 60.0


def _fetch_law_count() -> int | None:
    """查询知识库中法律/行政法规/司法解释数量（不含案例）

    Returns:
        文档数量；查询失败返回 None（调用方回退到默认值）
    """
    import time
    now = time.time()
    cached = _capability_count_cache.get("count")
    if cached is not None and now - _capability_count_cache.get("ts", 0) < _CAPABILITY_COUNT_TTL:
        return cached

    count = None
    try:
        from src.config import PG_CONN
        import psycopg2
        conn = psycopg2.connect(PG_CONN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM documents "
                    "WHERE doc_type IN ('law', 'regulation', 'judicial_interpretation', 'constitution')"
                )
                row = cur.fetchone()
                count = int(row[0]) if row else None
        finally:
            conn.close()
        _capability_count_cache["count"] = count
        _capability_count_cache["ts"] = now
    except Exception:
        # 查询失败不阻塞能力回复，回退默认值
        count = None
    return count


def get_capability_reply() -> str:
    """能力问句的固定回复（法律数量动态取自知识库）

    Returns:
        带实际法律数量的能力清单文本
    """
    count = _fetch_law_count()
    if count is None or count <= 0:
        count = "900+"
    return _CAPABILITY_REPLY_TEMPLATE.format(count=count)

# 法律关键词 — 包含任一即走检索
_LEGAL_KEYWORDS = [
    # 法律概念
    "法律", "法条", "法规", "条文", "条款", "规定（法律",
    # 处罚
    "处罚", "罚款", "拘留", "判刑", "刑期", "有期徒刑",
    "无期徒刑", "死刑", "拘役", "管制", "没收", "吊销",
    # 责任赔偿
    "赔偿", "责任", "侵权", "违约", "损害", "损失",
    # 权利
    "权利", "义务", "隐私", "名誉", "肖像", "人身",
    # 法律关系
    "合同", "协议", "婚姻", "离婚", "继承", "遗嘱",
    "收养", "抚养", "赡养", "劳动", "社保", "工伤",
    # 诉讼
    "诉讼", "仲裁", "起诉", "上诉", "判决", "裁定", "执行",
    "证据", "时效", "管辖", "法院",
    # 犯罪
    "犯罪", "罪名", "故意", "过失", "自首", "累犯",
    "盗窃", "诈骗", "抢劫", "伤害", "杀人",
    # 法律名称简称
    "民法典", "刑法", "宪法", "公司法", "劳动法",
    "治安管理", "道路交通", "行政法", "刑事法",
    # 法律问句模式
    "怎么罚", "判多久", "合法吗", "违法吗", "要不要赔",
    "能告吗", "算不算", "有没有责任",
]


# ---------------------------------------------------------------------------
# Prompt 注入防御：输入安全过滤
# ---------------------------------------------------------------------------

# Prompt 注入攻击模式（命中任一即拒绝请求）
_INJECTION_PATTERNS = [
    # System prompt 泄露 / 越狱
    r'忽略.*(指令|规则|限制|prompt|system|提示)',
    r'(ignore|forget|disregard).*(instruction|rule|prompt|system)',
    r'你.*(是|现在|扮演|作为).*(一个|新的).*(角色|身份)',
    r'DAN\b|jailbreak|越狱',
    r'(print|show|display|reveal|输出).*(system.?prompt|instructions|提示词|系统指令)',
    r'repeat\s+(after\s+me|the\s+following|this)',
    # 角色劫持
    r'(现在开始|从现在起|从今以后).*(你是|你叫|你变成)',
    r'你不再是.*你是',
    r'forget\s+(all|everything).*(before|previous|above)',
    # Token 泄露
    r'(api.?key|secret|token|password|密码).*(告诉我|给我|显示|输出|是什么)',
    r'(what|where)\s+is\s+(your|the)\s+(api.?key|token|secret)',
]

# 敏感内容关键词（涉黄涉政，命中后拒绝服务）
_SENSITIVE_KEYWORDS = [
    # 政治敏感
    "习近平", "江泽民", "胡锦涛", "六四", "天安门", "法轮功",
    "台独", "藏独", "疆独", "港独",
    # 色情暴力
    "色情", "淫秽", "裸体", "性交", "强奸", "杀人方法",
]

# 拒绝回复模板
_INJECTION_REJECT_MSG = "该问题不在我的服务范围内，请提出合法的法律咨询问题。"
_SENSITIVE_REJECT_MSG = "该问题不在我的服务范围内。"


def sanitize_input(query: str) -> tuple[str, bool, str | None]:
    """输入安全过滤，返回 (清洗后文本, 是否安全, 拒绝原因)

    Args:
        query: 原始用户输入

    Returns:
        (清洗后文本, 是否安全, 拒绝原因或None)
        - 安全: 返回原文本 + True + None
        - 不安全: 返回拒绝消息 + False + 拒绝原因
    """
    if not query or not query.strip():
        return query, True, None

    q = query.strip()

    # 1. Prompt 注入检测
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            logger.warning(f"[安全] Prompt 注入拦截: pattern={pattern}, query_preview={q[:100]}")
            return _INJECTION_REJECT_MSG, False, "prompt_injection"

    # 2. 长度限制（防止资源耗尽攻击）
    if len(q) > 2000:
        logger.warning(f"[安全] 输入过长被截断: len={len(q)}")
        q = q[:2000]

    # 3. 敏感内容检测
    nq = _normalize(q)
    for kw in _SENSITIVE_KEYWORDS:
        if _normalize(kw) in nq:
            logger.warning(f"[安全] 敏感词拦截: keyword={kw}, query_preview={q[:100]}")
            return _SENSITIVE_REJECT_MSG, False, "sensitive_content"

    return q, True, None


def _normalize(text: str) -> str:
    """标准化：去标点、去空格、小写"""
    return re.sub(r'[^\w\u4e00-\u9fff]', '', text.lower().strip())


# ---------------------------------------------------------------------------
# 闲聊判定（正则，供 RAGEngine）
# ---------------------------------------------------------------------------

def is_casual_query(query: str) -> bool:
    """快速正则判断是否为明显的闲聊/问候（用于响应元数据标记）"""
    q = query.strip().lower()
    if not q:
        return True
    for pattern in _CASUAL_PATTERNS:
        if re.match(pattern, q):
            return True
    return False


# ---------------------------------------------------------------------------
# 意图分类（关键词，供 LangGraph Agent）
# ---------------------------------------------------------------------------

def _contains_legal_keyword(text: str) -> bool:
    """文本是否含法律关键词（能力问句判断时的排除条件）"""
    ntext = _normalize(text)
    return any(_normalize(kw) in ntext for kw in _LEGAL_KEYWORDS)


def is_capability_query(query: str) -> bool:
    """是否为"你能做什么/你有什么功能"类能力问句

    命中此类问句时，应返回系统固定的能力清单（而非让 LLM 自由发挥），
    避免 LLM 编造本系统不具备的能力（写代码、翻译、作图等）。

    识别策略（三级）：
      1. 严格变体：以"你能/你会/你有什么…"等固定开头，覆盖主流问法
      2. 宽松组合正则：结构化解耦"主语+情态词+动作+宾语+语气词"，
         覆盖"你会做什么？""你会做什么吗""你能干些啥""你都会啥"等变体
      3. 关键词兜底：以"你/您"为主语 + 含能力关键词
      （"我能做什么""打架能做什么""你能帮我查劳动法"等不命中）
    """
    q = query.strip()
    if not q:
        return False
    # 去除标点/空白，避免"你会做什么？""你会做什么吗"等匹配失败
    qq = re.sub(r'[。？！?!，,、~～\s“”"\'（）()]+', '', q)

    # 1. 严格变体
    for pattern in _CAPABILITY_PATTERNS:
        if re.match(pattern, q):
            return True

    # 2. 宽松组合正则（结构化解耦，覆盖语气词/变体组合）
    for pattern in _CAPABILITY_LOOSE:
        if re.match(pattern, qq):
            if _contains_legal_keyword(qq):
                return False
            return True

    # 3. 关键词兜底：主语限定"你/您"，避免误伤"我能做什么""打架能做什么"
    if re.match(r'^[你您]', q):
        if any(kw in q for kw in _CAPABILITY_KEYWORDS):
            if _contains_legal_keyword(qq):
                return False
            return True
    return False


def classify_intent(query: str, history: list | None = None) -> bool:
    """意图识别：是否为法律相关问题？

    1. 正则兜底：明显闲聊 → 闲聊
    2. 身份/自我介绍问句（"我是谁"、"我是痕至"、"你记得我吗"）→ 闲聊
    3. 标准化后精确匹配闲聊短语 → 闲聊
    4. 标准化后包含法律关键词 → 法律
    5. 短查询（≤4字）包含闲聊短语 → 闲聊（二次检查）
    6. 结合对话历史：用户刚问候/自我介绍后紧跟的短句 → 闲聊（延续性）
    7. 都不匹配 → 默认检索（宁可多检）

    Args:
        query: 用户输入
        history: 可选，多轮对话历史（list[dict] 或 list[Message]），
                 用于判断"我是谁""你记得我吗"等依赖上下文的延续性闲聊。
    """
    q = query.strip()
    nq = _normalize(q)

    if q and is_casual_query(query):
        return False

    # 身份/自我介绍问句（独立于 is_casual_query，避免影响 engine 快路径）
    for pattern in _SELF_INTRO_PATTERNS:
        if re.match(pattern, q, re.IGNORECASE):
            return False

    for phrase in _CASUAL_PHRASES:
        if _normalize(phrase) == nq:
            return False

    for kw in _LEGAL_KEYWORDS:
        if _normalize(kw) in nq:
            return True

    if len(nq) <= 4:
        for phrase in _CASUAL_PHRASES:
            if _normalize(phrase) in nq:
                return False

    # 上下文延续：历史上用户刚问候/自我介绍，当前为短句 → 延续性闲聊
    if history and _is_contextual_casual(query, history):
        return False

    return True


# 案例/判例关键词 — 命中则分类为案例查询
# 注：匹配在 _normalize 后做子串查找，不支持正则。
# 因此复合模式（如"有没有{任意}案子"）需拆分为独立原子关键词，
# 任一命中即判定为 case_query。
_CASE_KEYWORDS = [
    # 直接指代
    "案例", "判例", "判决书", "判决", "类案", "先例",
    # 组合 — 案例查询常见前缀/中缀
    "指导案例", "典型案件", "最高法案例",
    # 用户口语提问模式（原子化拆分以覆盖"有没有{任意}案子"等句式）
    "有没有",      # 覆盖：有没有{类似盗窃的}案子
    "类似",        # 覆盖：类似{抢劫}的案子、类似{打人}怎么判
    "类似案件",    # 覆盖面兜底
    "怎么判的",    # 覆盖：{这种情况}怎么判的
    "怎么判",      # 覆盖：{盗窃}怎么判
    "有什么案例",  # 覆盖：有什么{相关}案例
    # 三大诉讼类型
    "刑事案件", "民事案件", "行政案件",
    # 口语动作
    "打官司", "翻案",
]


def classify_query_type(query: str, history: list | None = None) -> str:
    """意图三分类：返回查询类型

    Args:
        query: 用户输入
        history: 可选，多轮对话历史，用于延续性闲聊判断

    Returns:
        "casual"       — 闲聊/问候，不检索
        "case_query"   — 案例查询，走案例检索路由
        "law_lookup"   — 法律条文查询，走法条检索路由
    """
    q = query.strip()
    nq = _normalize(q)

    # 0. 安全过滤失败 → 特殊处理
    _, is_safe, _ = sanitize_input(q)
    if not is_safe:
        return "casual"

    # 1. 闲聊检测（含身份/自我介绍 + 历史延续性判断）
    if not classify_intent(q, history=history):
        return "casual"

    # 2. 案例关键词检测（已去重 normalize，直接在 nq 上匹配）
    for kw in _CASE_KEYWORDS:
        if _normalize(kw) in nq:
            return "case_query"

    # 3. 包含法律关键词 → 法条查询
    for kw in _LEGAL_KEYWORDS:
        if _normalize(kw) in nq:
            return "law_lookup"

    # 4. 默认：长查询走法条检索
    return "law_lookup"


def _history_suggests_casual(history: list) -> bool:
    """历史中最近的用户消息是问候或自我介绍（供延续性闲聊判断）

    跳过中间的助手回复,从后往前找最近一条 user 消息。
    修复：当 history[-1] 是 AI 回复时不会直接放弃判断。
    """
    if not history:
        return False
    for last in reversed(history):
        if isinstance(last, dict):
            role = last.get("role", "")
            content = str(last.get("content", ""))
        else:
            role = getattr(last, "role", "")
            content = str(getattr(last, "content", ""))
        if role != "user":
            continue
        c = content.strip()
        if not c:
            return False
        return bool(
            re.match(r'^(你好|您好|hi|hello|嗨|嗨喽)', c, re.IGNORECASE)
            or re.match(r'^(我是|我叫|我姓)', c)
        )
    return False


def _is_contextual_casual(query: str, history: list) -> bool:
    """当前为短句、不含法律关键词，且历史上用户刚问候/自我介绍 → 延续性闲聊

    例：用户先发"你好，我是痕至"，紧接着问"我是谁/你记得我吗/那我呢"。
    含法律关键词的短句（如"判多久"）仍按法律咨询处理。
    """
    nq = _normalize(query)
    if not nq or len(nq) > 8:
        return False
    for kw in _LEGAL_KEYWORDS:
        if _normalize(kw) in nq:
            return False
    return _history_suggests_casual(history)


# ---------------------------------------------------------------------------
# LLM 自省路由：判断是否需要检索
# ---------------------------------------------------------------------------

ROUTE_PROMPT = """判断以下用户消息是否需要用法律知识库检索来回答。

## 规则
- YES: 涉及法律条文、法规、处罚、程序、权利等法律专业知识
- NO: 问候、感谢、告别、自我介绍、纯闲聊、日常对话

只输出 YES 或 NO，不要解释。

用户消息: {query}"""


def needs_retrieval(query: str, llm) -> bool:
    """LLM 自省：是否需要检索法律知识库？

    1. 正则命中 → 明确闲聊，不检索
    2. 问题超过 8 个字 → 大概率法律问题，直接检索（零延迟）
    3. 短模糊查询 → LLM 自省判断（正则误杀和真实闲聊的中间地带）
    """
    if is_casual_query(query):
        return False

    # 长查询大概率是正经问题，不走 LLM 路由省一次调用
    if len(query.strip()) > 8:
        return True

    # 短模糊查询：LLM 判断
    prompt = ROUTE_PROMPT.format(query=query)
    result = llm.chat(
        prompt,
        system_prompt="你是一个查询路由判断器。只输出 YES 或 NO。",
    ).strip().upper()

    if "NO" in result:
        return False
    return True
