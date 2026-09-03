"""
用量计费存储（M4 / F15）：usage_logs 明细落库 + 价格表读写 + 聚合查询。

设计要点（对照 docs/F15-日志与Token计费面板-技术方案.md）：
- **旁路观测**：所有写失败 debug 级吞掉——观测组件故障绝不拖垮主链路
  （与 query_log / cost_budget 同一原则）；所有读失败降级返回空/默认值。
- **连接池复用**：每次操作从 `src.db.pool` 借连接（`db_connection()`），
  池不可用退化直连——不在本模块持有常驻连接。
- **金额快照**：`record_usage` 落库时按**当时价格表**算好 `cost_cny`，
  改价不漂移历史；原始 token / credits 同时保留，可随时按新价重算。
- **价格表**：config 默认值（`PRICING_DEFAULTS`）→ 首启灌入 pricing 表 →
  前端动态编辑。内存缓存（进程级）+ `updated_at` 失效，读优先表、表空/故障
  回退 config 默认。写价格 = 直接写表并失效缓存。
- **不建 rollup（D-F15-5）**：聚合查询直接 GROUP BY 明细表；将来量大可
  换物化视图/定时表，只需改 read_usage_* 内部，不改 API。
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# 来源常量（与 cost_budget 对齐）
SOURCE_LLM = "llm"
SOURCE_TAVILY = "tavily"
SOURCE_PKULAW = "pkulaw"
_SOURCES = (SOURCE_LLM, SOURCE_TAVILY, SOURCE_PKULAW)

# pkulaw purpose → 价格表键（官方积分：search 语义 125 / keyword 精确 25 / recognition 识别 125）
# ⚠️ purpose 是 PkulawMCPClient._run(purpose, ...) 的实际入参取值，不是工具展示名
_PKULAW_TOOL_POINTS_KEY = {
    "article_search": "pkulaw.search.points_per_call",  # 法条语义检索（search_article 走这）
    "case_search": "pkulaw.search.points_per_call",  # 类案语义检索（search_case 走这）
    "article_exact": "pkulaw.keyword.points_per_call",  # 法条精确取条（get_article）
    "law_list": "pkulaw.keyword.points_per_call",  # 法规关键词列表（get_law_list）
    "verify_law": "pkulaw.recognition.points_per_call",  # 法条识别溯源
    "verify_case": "pkulaw.recognition.points_per_call",  # 案号识别溯源
    "verify_provision": "pkulaw.recognition.points_per_call",  # 法条核验对照
    "add_links": "pkulaw.recognition.points_per_call",  # 超链
}

# ---------------------------------------------------------------------------
# 进程级价格缓存
# ---------------------------------------------------------------------------

_price_cache: dict[str, float] | None = None
_price_cache_lock = threading.Lock()


def _reset_price_cache() -> None:
    """清空价格缓存（测试 / upsert 后调用）。"""
    global _price_cache
    with _price_cache_lock:
        _price_cache = None


def _load_prices_from_db() -> dict[str, float]:
    """从 pricing 表读全量价格（成功返回表内容；故障/空表返回 {} 由上层回退默认）。"""
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM pricing")
                rows = cur.fetchall()
        return {str(k): float(v) for k, v in rows} if rows else {}
    except Exception as e:
        logger.debug(f"读取价格表失败（回退 config 默认）: {e}")
        return {}


def _merged_prices() -> dict[str, float]:
    """合并后的价格：pricing 表覆盖值优先，缺省回退 config PRICING_DEFAULTS。

    结果按 key 缓存（进程级），表故障时直接返回默认集。
    """
    global _price_cache
    if _price_cache is None:
        with _price_cache_lock:
            if _price_cache is None:
                try:
                    from src.config import PRICING_DEFAULTS

                    merged = {k: float(v["value"]) for k, v in PRICING_DEFAULTS.items()}
                    db_overrides = _load_prices_from_db()
                    if db_overrides:
                        merged.update(db_overrides)
                    _price_cache = merged
                except Exception as e:  # pragma: no cover - 防御
                    logger.warning(f"价格合并失败: {e}")
                    _price_cache = {}
    return _price_cache


def _db_keys() -> set[str]:
    """当前 pricing 表中存在的键（覆盖了默认值的）。读取失败返回空集。"""
    return set(_load_prices_from_db().keys())


def get_price(key: str) -> float:
    """取某个价格键的当前值（表覆盖优先，config 默认兜底；未知键返回 0）。"""
    return float(_merged_prices().get(key, 0.0) or 0.0)


# ---------------------------------------------------------------------------
# 写入：usage_logs
# ---------------------------------------------------------------------------


def record_usage(
    *,
    source: str,
    model: str,
    tool: str | None = None,
    backend: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    credits: int = 0,
    est: bool = False,
    cost_cny: float = 0.0,
    user_id: str = "default",
    request_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """落一行用量明细（金额快照由调用方按当时价格表算好传入）。

    任何异常 debug 级吞掉——观测组件故障绝不拖垮主链路。
    """
    if source not in _SOURCES:
        return
    day = date.today()
    total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO usage_logs "
                    "(day, user_id, request_id, session_id, source, model, tool, backend, "
                    " prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens, "
                    " total_tokens, credits, est, cost_cny) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        day,
                        user_id,
                        request_id,
                        session_id,
                        source,
                        model,
                        tool,
                        backend,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        int(cache_hit_tokens or 0),
                        int(cache_miss_tokens or 0),
                        total_tokens,
                        int(credits or 0),
                        bool(est),
                        float(cost_cny or 0.0),
                    ),
                )
            conn.commit()
    except Exception as e:
        logger.debug(f"usage_logs 写入失败（忽略）: {e}")


def record_llm_usage(
    *,
    model: str,
    backend: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    est: bool = False,
    user_id: str = "default",
    request_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """LLM 调用落库：按后端/模型算好金额（DeepSeek 拆缓存，Ollama 免费）。"""
    cost = llm_cost_cny(
        model=model,
        backend=backend,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )
    record_usage(
        source=SOURCE_LLM,
        model=model,
        backend=backend,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        est=est,
        cost_cny=cost,
        user_id=user_id,
        request_id=request_id,
        session_id=session_id,
    )


def record_tavily_usage(
    *,
    depth: str = "basic",
    user_id: str = "default",
    request_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Tavily 搜索落库（按 depth 折算 credits → 金额）。失败 debug 吞掉。"""
    depth = str(depth or "basic").lower()
    credits = int(get_price(f"tavily.{depth}.credits_per_call") or 0)
    record_usage(
        source=SOURCE_TAVILY,
        model="tavily-search",
        tool=depth,
        credits=credits,
        cost_cny=tavily_cost_cny(credits),
        user_id=user_id,
        request_id=request_id,
        session_id=session_id,
    )


def record_pkulaw_usage(
    *,
    purpose: str,
    user_id: str = "default",
    request_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """北大法宝 MCP 落库（按 purpose 折算积分 → 金额）。失败 debug 吞掉。"""
    purpose = str(purpose or "")
    credits = pkulaw_credits_for_tool(purpose)
    record_usage(
        source=SOURCE_PKULAW,
        model=f"pkulaw-{purpose}" if purpose else "pkulaw",
        tool=purpose or None,
        credits=credits,
        cost_cny=pkulaw_cost_cny(credits),
        user_id=user_id,
        request_id=request_id,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# 计价（金额计算与价格表键的映射收敛于此，埋点统一调用）
# ---------------------------------------------------------------------------


def llm_cost_cny(
    *,
    model: str,
    backend: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
) -> float:
    """LLM 金额（元）：按 backend/model 取单价。

    - deepseek：输入拆 缓存命中/未命中 两档（价差 50 倍，不拆会高估近一倍）；
    - ollama / 其它本地后端：0（免费）；
    - 未命中数缺省 = prompt - hit（调用方没给 miss 时兜底）。
    """
    base = str(backend or "").lower()
    name = str(model or "").lower()
    # 本地/免费后端：ollama、local，或模型名像本地模型(qwen/llama)但不是 deepseek
    is_local_name = ("qwen" in name or "llama" in name) and "deepseek" not in name
    if base in ("ollama", "local") or is_local_name:
        return 0.0
    if base == "deepseek" or "deepseek" in name:
        hit = max(0, int(cache_hit_tokens or 0))
        miss = max(0, int(cache_miss_tokens or 0))
        if miss == 0 and hit == 0:
            miss = max(0, int(prompt_tokens or 0))
        out = max(0, int(completion_tokens or 0))
        return round(
            (hit / 1e6) * get_price("llm.deepseek.input_hit_cny_per_m")
            + (miss / 1e6) * get_price("llm.deepseek.input_miss_cny_per_m")
            + (out / 1e6) * get_price("llm.deepseek.output_cny_per_m"),
            6,
        )
    return 0.0


def pkulaw_credits_for_tool(tool: str) -> int:
    """北大法宝某工具单次消耗积分（价格表可改）。未知工具默认按 recognition 档。"""
    key = _PKULAW_TOOL_POINTS_KEY.get(tool, "pkulaw.recognition.points_per_call")
    return int(get_price(key) or 0)


def pkulaw_cost_cny(credits: int) -> float:
    """北大法宝金额（元）= 积分 × 元/积分（point_cny 可在前端按套餐改）。"""
    return round(int(credits or 0) * get_price("pkulaw.point_cny"), 6)


def tavily_cost_cny(credits: int) -> float:
    """Tavily 金额（元）= credits × credit 单价（PAYG 价；免费额度内仅估算参考）。"""
    return round(int(credits or 0) * get_price("tavily.credit_cny"), 6)


# ---------------------------------------------------------------------------
# 读取：聚合查询（纯 SQL GROUP BY，不建 rollup）
# ---------------------------------------------------------------------------


def read_usage_summary(days: int = 7) -> list[dict]:
    """按日聚合：{day, cost_cny, llm_calls, tavily_calls, pkulaw_calls,
    tokens_in, tokens_out, est_cost}。返回含今天的最近 days 天（无数据补 0）。"""
    days = max(1, min(int(days or 7), 90))
    rows: dict[str, dict] = {}
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT day, "
                    "  COALESCE(SUM(cost_cny),0) AS cost, "
                    "  COUNT(*) FILTER (WHERE source='llm') AS llm_calls, "
                    "  COUNT(*) FILTER (WHERE source='tavily') AS tavily_calls, "
                    "  COUNT(*) FILTER (WHERE source='pkulaw') AS pkulaw_calls, "
                    "  COALESCE(SUM(prompt_tokens) FILTER (WHERE source='llm'),0) AS tokens_in, "
                    "  COALESCE(SUM(completion_tokens) FILTER (WHERE source='llm'),0) AS tokens_out, "
                    "  COALESCE(SUM(cost_cny) FILTER (WHERE est),0) AS est_cost "
                    "FROM usage_logs "
                    "WHERE day >= CURRENT_DATE - %s::int "
                    "GROUP BY day ORDER BY day",
                    (days - 1,),
                )
                for day, cost, llm_calls, tavily_calls, pkulaw_calls, tin, tout, est_cost in cur.fetchall():
                    rows[str(day)] = {
                        "day": str(day),
                        "cost_cny": round(float(cost or 0), 6),
                        "llm_calls": int(llm_calls or 0),
                        "tavily_calls": int(tavily_calls or 0),
                        "pkulaw_calls": int(pkulaw_calls or 0),
                        "tokens_in": int(tin or 0),
                        "tokens_out": int(tout or 0),
                        "est_cost": round(float(est_cost or 0), 6),
                    }
    except Exception as e:
        logger.debug(f"usage summary 读取失败（返回空）: {e}")
    # 补齐最近 days 天（含今天），保证趋势图连续
    out: list[dict] = []
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        row = rows.get(d) or {
            "day": d,
            "cost_cny": 0.0,
            "llm_calls": 0,
            "tavily_calls": 0,
            "pkulaw_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "est_cost": 0.0,
        }
        out.append(row)
    return out


def read_usage_detail(day: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """明细分页（默认当天；按 ts 倒序）。"""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    try:
        from src.db.pool import db_connection

        target = day or date.today().isoformat()
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, user_id, request_id, session_id, source, model, tool, backend, "
                    "       prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens, "
                    "       total_tokens, credits, est, cost_cny "
                    "FROM usage_logs WHERE day=%s ORDER BY ts DESC LIMIT %s OFFSET %s",
                    (target, limit, offset),
                )
                cols = [
                    "ts",
                    "user_id",
                    "request_id",
                    "session_id",
                    "source",
                    "model",
                    "tool",
                    "backend",
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_hit_tokens",
                    "cache_miss_tokens",
                    "total_tokens",
                    "credits",
                    "est",
                    "cost_cny",
                ]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.debug(f"usage detail 读取失败（返回空）: {e}")
        return []


def read_usage_breakdown(days: int = 7, group: str = "source") -> list[dict]:
    """归因聚合：按 source / model / tool 分组统计费用与次数。"""
    days = max(1, min(int(days or 7), 90))
    group_col = group if group in ("source", "model", "tool") else "source"
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {group_col}, "
                    "  COALESCE(SUM(cost_cny),0) AS cost, "
                    "  COUNT(*) AS calls, "
                    "  COALESCE(SUM(total_tokens),0) AS tokens "
                    f"FROM usage_logs WHERE day >= CURRENT_DATE - %s::int "
                    f"GROUP BY {group_col} ORDER BY cost DESC",
                    (days - 1,),
                )
                return [
                    {
                        "key": str(g or "unknown"),
                        "cost_cny": round(float(cost or 0), 6),
                        "calls": int(calls or 0),
                        "tokens": int(tokens or 0),
                    }
                    for g, cost, calls, tokens in cur.fetchall()
                ]
    except Exception as e:
        logger.debug(f"usage breakdown 读取失败（返回空）: {e}")
        return []


# ---------------------------------------------------------------------------
# 价格表读写（pricing 表 + config 默认合并展示）
# ---------------------------------------------------------------------------


def list_pricing() -> list[dict]:
    """读价格表（含默认值说明），供前端展示与编辑：覆盖值标 source=db，其余 source=default。"""
    try:
        from src.config import PRICING_DEFAULTS

        defaults = {k: float(v["value"]) for k, v in PRICING_DEFAULTS.items()}
    except Exception:  # pragma: no cover - 防御
        defaults = {}
    db_overrides = _load_prices_from_db()
    out = []
    for key in sorted(defaults):
        out.append(
            {
                "key": key,
                "value": round(float(db_overrides.get(key, defaults[key])), 8),
                "unit": _unit_of(key),
                "source": "db" if key in db_overrides else "default",
            }
        )
    return out


def _unit_of(key: str) -> str:
    try:
        from src.config import PRICING_DEFAULTS

        return str(PRICING_DEFAULTS.get(key, {}).get("unit", "cny"))
    except Exception:
        return "cny"


def upsert_pricing(items: list[dict]) -> int:
    """写价格表覆盖值（body: [{key, value}]）。成功返回写入条数并失效缓存。

    只允许更新已知默认键；未知键静默忽略（防脏数据）。
    """
    if not items:
        return 0
    from src.config import PRICING_DEFAULTS

    valid = [(str(it["key"]), float(it["value"])) for it in items if it.get("key") in PRICING_DEFAULTS]
    if not valid:
        return 0
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                for key, value in valid:
                    cur.execute(
                        "INSERT INTO pricing (key, value, unit, note) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                        (key, value, _unit_of(key), PRICING_DEFAULTS[key].get("note", "")),
                    )
            conn.commit()
        _reset_price_cache()
        return len(valid)
    except Exception as e:
        logger.debug(f"价格表写入失败: {e}")
        return 0


def reset_pricing() -> None:
    """恢复默认：清空表内全部覆盖值（下次合并即回落 config 默认）。"""
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pricing")
            conn.commit()
        _reset_price_cache()
    except Exception as e:
        logger.debug(f"价格表重置失败: {e}")


def ensure_pricing_defaults() -> None:
    """首启灌入：把 config 默认值写入 pricing 表（幂等，仅当表为空时）。

    供应用启动时调用一次（api/main.py lifespan）；非空跳过。
    """
    try:
        from src.db.pool import db_connection

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM pricing")
                if int(cur.fetchone()[0]) > 0:
                    return
                from src.config import PRICING_DEFAULTS

                for key, cfg in PRICING_DEFAULTS.items():
                    cur.execute(
                        "INSERT INTO pricing (key, value, unit, note) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (key) DO NOTHING",
                        (key, float(cfg["value"]), cfg.get("unit", "cny"), cfg.get("note", "")),
                    )
            conn.commit()
        _reset_price_cache()
    except Exception as e:
        logger.debug(f"价格表灌入默认值失败（忽略，后续走 config 默认）: {e}")
