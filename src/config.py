"""
统一配置模块。

从 .env 文件和环境变量加载所有可配参数，提供一站式配置入口。
模块级变量可直接 from src.config import xxx 使用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# 自动加载项目根目录的 .env 文件
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ---- 日志 ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# 强制离线模式 — 必须在任何 HuggingFace 相关 import 之前设置
# sentence_transformers 5.x 的某些版本不完全尊重 HF_HUB_OFFLINE，
# 所以这里同时设三个环境变量 + 后续传给 CrossEncoder 的 local_files_only
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"


def _safe_float(key: str, default: float) -> float:
    """安全获取浮点型环境变量，格式错误时使用默认值并警告"""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning(f"环境变量 {key}='{val}' 不是合法浮点数，使用默认值 {default}")
        return default


def _safe_int(key: str, default: int) -> int:
    """安全获取整型环境变量，格式错误时使用默认值并警告"""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning(f"环境变量 {key}='{val}' 不是合法整数，使用默认值 {default}")
        return default


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

# 后端类型：ollama | openai
# 未设置 EMBED_BACKEND 时也会回退到此值
# M1（F5）：默认后端切外接 API（DeepSeek），Ollama 保留为降级后端
LLM_BACKEND = os.getenv("LLM_BACKEND", "openai")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")
LLM_TEMPERATURE = _safe_float("LLM_TEMPERATURE", 0.1)
LLM_TOP_P = _safe_float("LLM_TOP_P", 0.9)
LLM_MAX_TOKENS = _safe_int("LLM_MAX_TOKENS", 2048)
LLM_MAX_RETRIES = _safe_int("LLM_MAX_RETRIES", 3)

# 降级后端（REQ-U1 / REQ-UW2）：主后端不可用时回退 Ollama
LLM_FALLBACK_BACKEND = os.getenv("LLM_FALLBACK_BACKEND", "ollama")
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "qwen2.5:3b")
LLM_FALLBACK_BASE_URL = os.getenv("LLM_FALLBACK_BASE_URL", "http://localhost:11434")

# Ollama 请求上下文窗口 (num_ctx)。0 = 自动使用模型声明窗口 (get_context_window())。
# 注意：Ollama 服务端 num_ctx 默认仅 2048，不显式下发会导致输入被静默截断；
# 但窗口越大 KV Cache 显存占用越高，需按部署机显存调整。
OLLAMA_NUM_CTX = _safe_int("OLLAMA_NUM_CTX", 0)

# OpenAI 兼容后端配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
# M1 修正：deepseek-chat 已于 2026-07-24 弃用，默认改用 deepseek-v4-flash（非思考模式，function calling 完整支持）
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "deepseek-v4-flash")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# ---------------------------------------------------------------------------
# 流式 / 并发
# ---------------------------------------------------------------------------

# 并发 LLM 流上限：防止过多请求同时打向供应商导致 429 / 本地显存溢出。
# 超过上限的流式请求排队等待，前端会先看到"排队中"状态。
LLM_MAX_CONCURRENCY = _safe_int("LLM_MAX_CONCURRENCY", 8)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

# 后端类型：ollama | openai
# 未设置时回退到 LLM_BACKEND 的值
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "http://localhost:11434")
EMBED_BATCH_SIZE = _safe_int("EMBED_BATCH_SIZE", 32)
EMBED_MAX_RETRIES = _safe_int("EMBED_MAX_RETRIES", 3)


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

RETRIEVAL_TOP_K = _safe_int("RETRIEVAL_TOP_K", 5)

# 检索时是否过滤章级摘要 chunk（噪声大，评测已验证应过滤）
# 在检索层统一拦截，避免运行时出现 30+ 条无关条文被召回的问题
RETRIEVAL_DROP_SUMMARY_CHUNKS = os.getenv("RETRIEVAL_DROP_SUMMARY_CHUNKS", "true").lower() == "true"

# 向量相似度召回阈值（bge-m3 归一化内积，范围约 [-1, 1]，0.95≈强相关、<0.4 视为较差）。
# 仅作为召回质量闸门：向量分数低于阈值的结果被丢弃；若过滤后无候选则回退保留原结果（避免哑火）。
# 0.0 表示关闭（默认，保持评测指标 Recall@5=73% 不变）。建议启用值 0.3~0.5。
RETRIEVAL_SIM_THRESHOLD = _safe_float("RETRIEVAL_SIM_THRESHOLD", 0.0)

# Reranker 二次精排 (Cross-Encoder)。评测验证可显著提升召回质量、消除噪声；
# 纯 CPU 推理会增加少量延迟，有 GPU 更佳。默认开启以对齐评测验证过的配置。
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").lower() == "true"
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# 40（2026-08-31 转正，手册 §6.1 流程）：colloq148 Hit@5 73.0%→79.7%、
# multi100 92.0% 不回归且 MRR 0.7379→0.8006（docs/检索质量与响应性能评估-2026-08-31.md）。
# ⚠️ 必须 > RERANK_TOP_K：相等时 reranker.py 的短路返回会让 rerank 静默不执行
# （历史坑：15==15 导致生产精排长期未生效），RerankRetriever 构造时会打告警。
RERANK_RECALL_K = _safe_int("RERANK_RECALL_K", 40)  # 粗排召回数
RERANK_TOP_K = _safe_int("RERANK_TOP_K", 15)  # 精排后返回数
# rerank 打分输入单条截断：CrossEncoder 耗时对文本长度 O(n²)（实测 2000 字 40 对 ~20s）；
# 库内 chunk P99=480 字，800 字上限覆盖 99%+ 且耗时安全，只截打分输入不改返回内容
RERANK_MAX_CHARS = _safe_int("RERANK_MAX_CHARS", 800)

# 连续片段扩展：检索后自动拉取相邻 ±N 条条文
ADJACENT_ENABLED = os.getenv("ADJACENT_ENABLED", "true").lower() == "true"
# 相邻扩展窗口：原默认 ±3 会把每条命中扩展成 7 条，引用列表被大量
# "相邻但不相关"的条文污染；±1 仅保留紧邻上下文（引用仍以检索命中为主）
ADJACENT_WINDOW = _safe_int("ADJACENT_WINDOW", 1)  # ±N 条

# BM25 关键词检索（rank-based 混合，条件激活）
# 设计：BM25 只用"返回顺序（排名）"参与加权 RRF 融合，不碰向量分数——
# 与向量语义检索（按分数）逻辑互补。仅当查询含明确法律实体（法名/条款号）时激活，
# 通用语义查询保持纯向量，零干扰。
# 法条级评测验证：条件混合(w=3.0) 在 339 条"法名+条号/法名+关键词"查询上
# Hit@5 75.8%→86.1%、Hit@10 82.0%→91.7%（旧语义集 100 条上持平 69%）。
HYBRID_ENABLED = os.getenv("HYBRID_ENABLED", "true").lower() == "true"
HYBRID_RRF_K = _safe_int("HYBRID_RRF_K", 60)  # RRF 常数
# 0.5（2026-08-29 双集实测定值）：w=3.0 会让 BM25 词面排序在「法名+语义」
# 查询上碾压向量排名（语义集净丢 6 条，见 docs/向量路质量排查-2026-08-29.md）；
# w=0.5 时语义集 67% / 法条级 85.3%，两集最优平衡
HYBRID_BM25_WEIGHT = _safe_float("HYBRID_BM25_WEIGHT", 0.5)  # BM25 路权重（向量=1.0）
# True = BM25 无条件参与融合（跳过法名/条款号识别）。默认 False（条件激活）：
# 是否常开需双集评测决定，见 docs/向量路质量排查-2026-08-29.md §5.4
HYBRID_ALWAYS_ON = os.getenv("HYBRID_ALWAYS_ON", "false").lower() == "true"

# BM25 索引启动预热：构建耗时实测 ~38s（50867 chunks，见
# docs/检索质量与响应性能评估-2026-08-31.md §4.3）。懒加载会把这 38s 落到
# **首次查询**上，是首响头号瓶颈。默认开启：启动时用后台线程提前构建，
# 服务不被阻塞，懒加载保留为兜底（load_index 内部有锁，并发安全）。
BM25_PRELOAD = os.getenv("BM25_PRELOAD", "true").lower() == "true"


# ---------------------------------------------------------------------------
# 向量索引
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LangGraph Agent
# ---------------------------------------------------------------------------

# LangGraph Agent 路径（含答案校验/自动重试）。默认关闭：
# 开启后每条查询会额外发起一次 validate LLM 调用，延迟上升；
# 追求更高回答质量（幻觉审核 + 自动重试）时可设为 true。检索质量与噪声过滤不依赖它。
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "false").lower() == "true"
AGENT_MAX_RETRIES = _safe_int("AGENT_MAX_RETRIES", 1)

# M1 工具调用型 Agent（F2 ReAct 循环）：
# AGENT_ENABLED=true 且 AGENT_REACT_ENABLED=true 时走 ReAct 图（agent⇄tools 循环）；
# 设为 false（或主后端降级为 Ollama）时回退现有固定管线图（AC-7 向后兼容）。
AGENT_REACT_ENABLED = os.getenv("AGENT_REACT_ENABLED", "true").lower() == "true"
# ReAct 工具调用轮数上限（REQ-UW4），达到上限强制产出答案
AGENT_MAX_TOOL_TURNS = _safe_int("AGENT_MAX_TOOL_TURNS", 5)

# 工具结果摘要长度上限（字符）：SSE tool_result.summary 展示 + LLM 回灌，控制上下文膨胀
TOOL_RESULT_SUMMARY_MAX_CHARS = _safe_int("TOOL_RESULT_SUMMARY_MAX_CHARS", 300)

# FAQ 过期缓存后台清理周期（小时）。服务启动后周期性执行 clean_expired()
FAQ_CLEAN_INTERVAL_HOURS = _safe_int("FAQ_CLEAN_INTERVAL_HOURS", 24)

# FAQ 缓存 TTL（小时）：超过即销毁；命中缓存时自动顺延刷新（热问题续命）
FAQ_TTL_HOURS = _safe_int("FAQ_TTL_HOURS", 1)

# FAQ 缓存后端: redis（Redis Stack，向量检索 + 原生 TTL）| pg（pgvector，定时清理）
# 推荐 redis：TTL 自动过期，无需后台清理任务；pg 为无 Redis 环境的回退方案
FAQ_CACHE_BACKEND = os.getenv("FAQ_CACHE_BACKEND", "redis").lower()

# Redis 连接串（Redis Stack：redis:// 或 rediss://）
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# F12 v1 人工确认标记 TTL（秒，Q7 决策：10 分钟超时，超时需重新发起）。
# 超时行为 = 取消（不自动按默认参数继续执行，B 类多为文书/合同，擅自代答有法律风险）
CONFIRMATION_TTL_SECONDS = _safe_int("CONFIRMATION_TTL_SECONDS", 600)

# D-M3-12 断线重连：SSE 事件日志保留时长（秒）。被动断线后生成继续跑完并写入，
# 重连在此时限内可补发；过期后重连接口返回 404
STREAM_LOG_TTL_SECONDS = _safe_int("STREAM_LOG_TTL_SECONDS", 600)

# ---------------------------------------------------------------------------
# B2 二阶段：法名推断软信号（docs/B2-法名推断spike报告-2026-08-30.md）
# ---------------------------------------------------------------------------

# 质心最近邻 top3 候选法名重排加权，仅无 法名/条号查询激活（软信号，不硬过滤）。
# 验证判据：colloq148 Hit@5 73%→≥80%，multi100 92% 不回归
LAW_NAME_BOOST_ENABLED = os.getenv("LAW_NAME_BOOST_ENABLED", "false").lower() == "true"
LAW_NAME_BOOST = _safe_float("LAW_NAME_BOOST", 0.1)
LAW_NAME_BOOST_TOP_LAWS = _safe_int("LAW_NAME_BOOST_TOP_LAWS", 3)

# 无法名口语查询的正交信号：LLM 改写 + 双路 RRF 融合（仅无 法名/条号查询触发）
REWRITE_FUSION_ENABLED = os.getenv("REWRITE_FUSION_ENABLED", "false").lower() == "true"
REWRITE_FUSION_RECALL_K = _safe_int("REWRITE_FUSION_RECALL_K", 20)
REWRITE_FUSION_RRF_K = _safe_int("REWRITE_FUSION_RRF_K", 60)

# ---------------------------------------------------------------------------
# 网络搜索（F3 / Tavily）
# ---------------------------------------------------------------------------

# Tavily API Key：由后端统一管理，宿主机 .env 提供，经 docker-compose 注入容器
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_MAX_RESULTS = _safe_int("TAVILY_MAX_RESULTS", 5)
TAVILY_TIMEOUT = _safe_float("TAVILY_TIMEOUT", 15.0)

# ---------------------------------------------------------------------------
# 官方法律源（M2 / F9）
# ---------------------------------------------------------------------------

# 官方法律源检索总开关：false 时不注册 legal_source_search 工具
LEGAL_SOURCE_ENABLED = os.getenv("LEGAL_SOURCE_ENABLED", "true").lower() == "true"
# 官方源 HTTP 超时（秒）——官方接口响应偏慢，独立于 Tavily 配置
LEGAL_SOURCE_TIMEOUT = _safe_float("LEGAL_SOURCE_TIMEOUT", 10.0)
# 单次官方源检索返回条数上限
LEGAL_SOURCE_MAX_RESULTS = _safe_int("LEGAL_SOURCE_MAX_RESULTS", 5)

# 小包公（第三方案例 API，可选）：Key 与接口地址都配置时才启用
XBG_API_KEY = os.getenv("XBG_API_KEY", "")
XBG_API_URL = os.getenv("XBG_API_URL", "")

# 北大法宝 MCP（M3+ / 官方法律源增强，F9 扩展）：
# 默认连聚合服务 mcp-law-agg（一个端点暴露全部 10 个工具）；
# 若聚合端点不可用，可改 PKULAW_MCP_URL 指向单个服务端点。
# URL 与 Bearer Token 均由宿主机 .env 提供，经 docker-compose 注入容器。
PKULAW_ENABLED = os.getenv("PKULAW_ENABLED", "true").lower() == "true"
# 聚合端点（含全部工具，工具名带服务前缀，运行时按用途解析）
PKULAW_MCP_URL = os.getenv(
    "PKULAW_MCP_URL",
    "https://apim-gateway.pkulaw.com/mcp-law-agg/mcp",
)
PKULAW_MCP_TOKEN = os.getenv("PKULAW_MCP_TOKEN", "")
# MCP 调用超时（秒）：北大法宝按积分计费，单次类案返回体极大，超时放宽
PKULAW_TIMEOUT = _safe_float("PKULAW_TIMEOUT", 30.0)
# 单次检索返回条数上限（经客户端约束，默认 5，避免案例体撑爆上下文）
PKULAW_MAX_RESULTS = _safe_int("PKULAW_MAX_RESULTS", 5)

# ---------------------------------------------------------------------------
# 双路融合（M2 / F6-F8）
# ---------------------------------------------------------------------------

# 融合后 sources 输出条数上限（SSE meta 与 FAQ 缓存引用）
FUSION_TOP_K = _safe_int("FUSION_TOP_K", 8)
# 来源权重（冲突裁决与排序）：内部库 1.0 > 官方源 0.85 > 网络 0.5×tavily_score
FUSION_WEIGHT_INTERNAL = _safe_float("FUSION_WEIGHT_INTERNAL", 1.0)
FUSION_WEIGHT_LEGAL = _safe_float("FUSION_WEIGHT_LEGAL", 0.85)
FUSION_WEIGHT_WEB = _safe_float("FUSION_WEIGHT_WEB", 0.5)
# 网络线索保底配额：纯按分排序时网络（≤0.5）会被权威来源（≥0.5）全部挤出 top_k，
# 导致 Tavily 调用了却一条都不展示给用户。设为 N 可保证至少 N 条网络线索进入结果
# （带 web_unverified 标注，用户仍能分辨可信度）。设 0 = 关闭配额，回归纯按分排序。
FUSION_WEB_MIN_SLOTS = _safe_int("FUSION_WEB_MIN_SLOTS", 2)

# ---------------------------------------------------------------------------
# 预算熔断（M3 / F14）
# ---------------------------------------------------------------------------

# 总开关：false 时完全不做统计与拦截（运维临时关闭用）
BUDGET_ENABLED = os.getenv("BUDGET_ENABLED", "true").lower() == "true"
# 每日 LLM 调用次数上限（0 = 不限制）。按"逻辑调用次数"计——一次 chat() 内
# 的 SDK 重试不重复计数，口径稳定且不受重试策略影响。
BUDGET_MAX_LLM_CALLS_PER_DAY = _safe_int("BUDGET_MAX_LLM_CALLS_PER_DAY", 5000)
# 每日 Tavily 搜索次数上限（0 = 不限制）。Tavily 按次计费，口径精确。
BUDGET_MAX_TAVILY_CALLS_PER_DAY = _safe_int("BUDGET_MAX_TAVILY_CALLS_PER_DAY", 500)
# 每日北大法宝 MCP 调用次数上限（0 = 不限制）。北大法宝按积分计费，精确按次计。
BUDGET_MAX_PKULAW_CALLS_PER_DAY = _safe_int("BUDGET_MAX_PKULAW_CALLS_PER_DAY", 200)
# 超限是否真的拦截：true = 抛 BudgetExceededError 熔断；false = 仅告警不阻断
# （上线观察期可先设 false，确认阈值合理后再打开）
BUDGET_ENFORCE = os.getenv("BUDGET_ENFORCE", "true").lower() == "true"

# ---------------------------------------------------------------------------
# pgvector
# ---------------------------------------------------------------------------

PG_ENABLED = os.getenv("PG_ENABLED", "false").lower() == "true"
# 安全基线：连接串默认值不含密码，生产环境必须通过 PG_CONN 环境变量显式提供
PG_CONN = os.getenv("PG_CONN", "postgresql://lawrag@localhost:5432/lawrag")

# ---------------------------------------------------------------------------
# 用量计费价格默认值（M4 / F15）
# ---------------------------------------------------------------------------
# 各外部付费 API 的单价默认值（单位：人民币）。设计（D-F15-4）：
# config 默认值 → 首启灌入 pricing 表 → 前端可动态编辑 → 落库时按当时价格算好
# cost_cny 快照（改价不漂移历史，明细里保留原始 token/积分可随时重算）。
# 价格查证日期：2026-09-03（DeepSeek 官方 api-docs.deepseek.com、Tavily
# docs.tavily.com、北大法宝 mcp.pkulaw.com 计价面板）。
# 键名约定：{source}.{scope}.{metric}_cny_per_m（每百万）| _cny（每单位）|
#            points_per_call（每次消耗积分）
# ⚠️ pricing 表有覆盖值时以表为准（usage_store 内存缓存优先表、回退此默认）。

# DeepSeek（元/百万 tokens，缓存命中比未命中便宜 50 倍——必须拆开否则高估近一倍）
PRICE_DEEPSEEK_INPUT_HIT_CNY_PER_M = 0.02
PRICE_DEEPSEEK_INPUT_MISS_CNY_PER_M = 1.0
PRICE_DEEPSEEK_OUTPUT_CNY_PER_M = 2.0
# Ollama 本地免费
PRICE_OLLAMA_INPUT_CNY_PER_M = 0.0
PRICE_OLLAMA_OUTPUT_CNY_PER_M = 0.0
# Tavily：PAYG $0.008/credit 折算（免费 1000 credits/月，学生更多；额度内实际不花钱，
# 面板按此单价展示估算金额并标注"免费额度内"，用户可改）
PRICE_TAVILY_CREDIT_CNY = 0.058
# Tavily 单次搜索消耗 credits（basic=1 / advanced=2，docs.tavily.com）
PRICE_TAVILY_BASIC_CREDITS = 1
PRICE_TAVILY_ADVANCED_CREDITS = 2
# 北大法宝：元/积分（充值档 ¥18/6000 积分起 ≈0.003，充得多单价更低，用户可改）
PRICE_PKULAW_POINT_CNY = 0.003
# 北大法宝每次调用消耗积分（官方计价：基础关键词类 25 / 语义·识别·超链·幻觉修正类 125）
PRICE_PKULAW_SEARCH_POINTS = 125
PRICE_PKULAW_KEYWORD_POINTS = 25
PRICE_PKULAW_RECOGNITION_POINTS = 125

# pricing 表首启灌入的默认键值（value / unit / note）
PRICING_DEFAULTS: dict[str, dict] = {
    "llm.deepseek.input_hit_cny_per_m": {
        "value": PRICE_DEEPSEEK_INPUT_HIT_CNY_PER_M,
        "unit": "cny",
        "note": "DeepSeek 官方刊例 2026-09-03",
    },
    "llm.deepseek.input_miss_cny_per_m": {
        "value": PRICE_DEEPSEEK_INPUT_MISS_CNY_PER_M,
        "unit": "cny",
        "note": "DeepSeek 官方刊例 2026-09-03",
    },
    "llm.deepseek.output_cny_per_m": {
        "value": PRICE_DEEPSEEK_OUTPUT_CNY_PER_M,
        "unit": "cny",
        "note": "DeepSeek 官方刊例 2026-09-03",
    },
    "llm.ollama.input_cny_per_m": {"value": 0.0, "unit": "cny", "note": "本地免费"},
    "llm.ollama.output_cny_per_m": {"value": 0.0, "unit": "cny", "note": "本地免费"},
    "tavily.credit_cny": {
        "value": PRICE_TAVILY_CREDIT_CNY,
        "unit": "cny",
        "note": "Tavily PAYG $0.008/credit 折算；免费额度内仅估算参考",
    },
    "tavily.basic.credits_per_call": {
        "value": PRICE_TAVILY_BASIC_CREDITS,
        "unit": "credit",
        "note": "basic 搜索 1 credit/次",
    },
    "tavily.advanced.credits_per_call": {
        "value": PRICE_TAVILY_ADVANCED_CREDITS,
        "unit": "credit",
        "note": "advanced 搜索 2 credits/次",
    },
    "pkulaw.point_cny": {
        "value": PRICE_PKULAW_POINT_CNY,
        "unit": "cny",
        "note": "北大法宝充值档折算（¥18/6000 起），可在前端按套餐改",
    },
    "pkulaw.search.points_per_call": {
        "value": PRICE_PKULAW_SEARCH_POINTS,
        "unit": "point",
        "note": "语义检索类 search_article/search_case ≈125 积分/次",
    },
    "pkulaw.keyword.points_per_call": {
        "value": PRICE_PKULAW_KEYWORD_POINTS,
        "unit": "point",
        "note": "关键词/精确类 get_article/get_law_list ≈25 积分/次",
    },
    "pkulaw.recognition.points_per_call": {
        "value": PRICE_PKULAW_RECOGNITION_POINTS,
        "unit": "point",
        "note": "识别溯源/超链/幻觉修正类 ≈125 积分/次",
    },
}

# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

HOST = os.getenv("HOST", "0.0.0.0")
PORT = _safe_int("PORT", 8000)


# 应用版本（2026-09-03 收敛为单一来源：与 pyproject.toml 保持一致，bump 只改一处）。
# 容器/无 pyproject 环境可经 APP_VERSION 覆盖。
APP_VERSION = os.getenv("APP_VERSION", "0.2.0")
