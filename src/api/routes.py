"""
API 路由定义。支持多轮对话 + LangGraph Agent + 用户会话隔离。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .dependencies import get_engine, get_agent, get_llm, _create_embedder
from .models import (
    ChatRequest,
    ChatResponse,
    CancelRequest,
    ConfirmRequest,
    HealthResponse,
    RegisterRequest,
    LoginRequest,
    AuthResponse,
    CrawlRequest,
    CrawlTaskResponse,
    CrawlStatusResponse,
    RewriteRequest,
)
from .auth import get_current_user, require_registered_user, register_user, login_user
from src.config import AGENT_ENABLED, LLM_MAX_CONCURRENCY
from src.memory.confirmation_store import get_confirmation_store
from src.observability.cost_budget import get_budget
from src.observability.stream_log import get_stream_log
from src.rag.engine import needs_retrieval
from src.rag.intent import sanitize_input, is_capability_query, get_capability_reply
from src.rag.scenes import KIND_B, get_scene
from src.llm.client import Message

router = APIRouter()
auth_router = APIRouter()
perf_logger = logging.getLogger("api.perf")
logger = logging.getLogger(__name__)


def _dicts_to_messages(history: list[dict]) -> list[Message]:
    return [Message(msg["role"], msg["content"]) for msg in history if msg.get("content")]


# 对话历史上限：最多保留轮数 / 单条最大字符数（防 token 放大与超大 body）
_HISTORY_MAX_TURNS = 10
_HISTORY_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# 频率限制（代码审查整改）：承载付费 LLM 调用的接口需要 IP 级滑动窗口限流。
#
# 为什么不用 slowapi / fastapi-limiter：项目无 Redis 强依赖（预算统计在 Redis
# 不可用时退进程内），引入外部限流库会把限流也绑到 Redis 上。这里用进程内
# 滑动窗口即可——单进程部署（Dockerfile 现状）下够用，多 worker 时退化为
# 「每 worker N 次/分钟」，仍远好于无限。
# ---------------------------------------------------------------------------
_RATE_WINDOWS: dict[str, tuple[str, list[float]]] = {}
_RATE_LOCK = threading.Lock()

# (最大次数, 窗口秒数) —— 按接口分别配置
_RATE_LIMITS = {
    "rewrite": (20, 60),  # 改写：每分钟 20 次（每次 1 次 LLM 调用）
}


def _client_ip(request: Request) -> str:
    """取客户端 IP（反向代理下优先 X-Forwarded-For 首跳）。

    注意：XFF 可伪造，仅在有可信反代时才应信任；这里只用于限流的粗粒度
    分桶，不作为鉴权依据。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return (request.client.host if request.client else "") or "unknown"


def _rate_limit(request: Request, bucket: str) -> None:
    """滑动窗口限流：超限时抛 429（带 Retry-After）。

    限流状态自身出任何问题都放行——监控类设施故障不拖垮主链路（D-M3-8 同款原则）。
    """
    max_calls, window = _RATE_LIMITS.get(bucket, (0, 60))
    if max_calls <= 0:
        return
    key = f"{bucket}:{_client_ip(request)}"
    now = time.monotonic()
    try:
        with _RATE_LOCK:
            _, hits = _RATE_WINDOWS.get(key, ("", []))
            hits = [t for t in hits if now - t < window]
            if len(hits) >= max_calls:
                retry_after = max(1, int(window - (now - hits[0])) + 1)
                raise HTTPException(
                    status_code=429,
                    detail=f"请求过于频繁，请 {retry_after} 秒后重试",
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)
            _RATE_WINDOWS[key] = (key, hits)
            # 顺手清理过期桶，防止长期运行内存膨胀（O(桶数)，桶数通常个位数）
            if len(_RATE_WINDOWS) > 512:
                for k in [k for k, (_, h) in _RATE_WINDOWS.items() if not h or now - h[-1] > window * 10]:
                    _RATE_WINDOWS.pop(k, None)
    except HTTPException:
        raise
    except Exception as e:  # 限流器自身故障 → 放行
        logger.warning(f"限流器异常（放行）: {e}")


def _budget_block_message() -> str:
    """F14：LLM 预算超限时给用户的提示文案；未超限返回空串。

    LLM 是生成回答的必需品，故整体熔断（Tavily 超限只停网络搜索，
    不影响主流程，见 web_search 工具内的降级处理）。
    """
    try:
        from src.observability.cost_budget import KIND_LLM, BudgetExceededError

        budget = get_budget()
        try:
            budget.check(KIND_LLM)
            return ""
        except BudgetExceededError as e:
            return (
                f"抱歉，系统今日的 AI 服务调用额度已用尽，暂时无法回答新的问题。\n\n"
                f"详情：{e}\n"
                f"如需紧急使用，请联系系统管理员调整额度或手动重置。"
            )
    except Exception as e:
        # 预算组件自身故障不应阻断服务（与"工具失败不抛异常"同一原则）
        logger.warning(f"预算检查失败（放行）: {e}")
        return ""


def _sanitize_history(history: list[dict] | None) -> list[dict]:
    """对话历史安全过滤 — 与 query 同级的注入防御。

    客户端可任意构造 history，若不过滤则 sanitize_input 对 query 的
    防御可被完全绕过。规则：
    - 仅保留 user/assistant 角色、字符串 content
    - 每条 content 过 sanitize_input，命中注入则丢弃该条（不整体拒绝，
      避免误伤正常长对话）
    - 限制最多 _HISTORY_MAX_TURNS 条、单条 _HISTORY_MAX_CHARS 字
    """
    if not history:
        return []
    safe: list[dict] = []
    for msg in history[-_HISTORY_MAX_TURNS:]:
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            continue
        cleaned, is_safe, _ = sanitize_input(content)
        if not is_safe:
            logger.warning("[history] 注入风险条目已丢弃: role=%s preview=%s", role, content[:50])
            continue
        safe.append({"role": role, "content": cleaned[:_HISTORY_MAX_CHARS]})
    return safe


@router.post("/rewrite")
def rewrite(
    req: RewriteRequest,
    request: Request,
    _user: str = Depends(require_registered_user),
):
    """查询改写：把口语化问题规范化为法律检索查询。

    该接口是"查询改写节点"的实现，由前端开关控制是否调用：
    - 关闭（法条精确查找）：前端不调用，直接用原句检索，保证绝对精确。
    - 开启（案情分析）：前端调用，将 `proposed_query` 展示给用户确认/编辑，
      确认后才用于 /chat/stream。改写风险由此转移为"用户确认过的意图"。

    鉴权说明（2026-09-01 审查整改）：本接口每次调用都会真实消耗一次 LLM，
    原来既无鉴权也无预算检查——匿名可无限刷，**直接绕过 F14 熔断**。现补三道闸：
    1) `require_registered_user`：401 拒绝匿名；
    2) `_budget_block_message()`：与 /api/chat 同口径的预算前置检查；
    3) `_rate_limit`：IP 级滑动窗口，防单账号短时高频。
    """
    from src.agents.rewrite import rewrite_query
    from src.rag.intent import sanitize_input

    safe_query, is_safe, _ = sanitize_input(req.query)
    if not is_safe:
        return {"proposed_query": req.query, "changed": False, "skipped": True}

    budget_msg = _budget_block_message()
    if budget_msg:
        perf_logger.warning("[rewrite] budget_exceeded: llm")
        return {"proposed_query": req.query, "changed": False, "skipped": True}

    _rate_limit(request, "rewrite")
    try:
        llm = get_llm()
        proposed = rewrite_query(llm, safe_query)
    except Exception as e:
        logger.warning("改写接口失败，回退原句: %s", e)
        return {"proposed_query": req.query, "changed": False, "skipped": True}
    changed = proposed.strip() != req.query.strip()
    return {"proposed_query": proposed, "changed": changed, "skipped": False}


@router.get("/budget")
def budget_status(_user: str = Depends(require_registered_user)):
    """F14：当日外部 API 用量与熔断状态（运维监控用，需登录）。

    鉴权说明（2026-09-01 审查整改）：原来用 `get_current_user`，它会在无 Token
    时回退匿名 user id、不拒绝请求——docstring 写着"需登录"实际匿名可读。
    改用 `require_registered_user`（硬鉴权，401）。

    返回 {enabled, enforce, date, storage, exceeded, detail:{llm, tavily}}，
    每项含 used / limit / remaining / exceeded。limit=0 表示不限制。
    """
    try:
        return get_budget().status()
    except Exception as e:
        logger.warning(f"预算状态查询失败: {e}")
        return {"enabled": False, "error": str(e)}


@router.get("/health", response_model=HealthResponse)
async def health():
    try:
        from src.config import LLM_MODEL

        eng = get_engine() if not AGENT_ENABLED else get_agent()

        # 遍历检索器链找到最内层的 pgvector retriever
        doc_count = 0
        index_ready = True
        retriever = getattr(eng, "retriever", None)
        if retriever:
            index_ready = retriever.is_ready()
            # 穿透装饰器链: AdjacentExpander → Reranker → PgvectorStoreRetriever
            chain = retriever
            while hasattr(chain, "_base"):
                chain = chain._base
            if hasattr(chain, "_store"):
                doc_count = getattr(chain._store, "doc_count", 0)

        return HealthResponse(
            status="ok",
            version="0.1.0",
            index_ready=index_ready,
            doc_count=doc_count,
            llm_model=LLM_MODEL,
        )
    except Exception as e:
        logger.error(f"[health] 引擎状态检查失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=503, detail="引擎未就绪，请稍后重试")


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    t_start = time.perf_counter()

    # 输入安全过滤（Prompt 注入 + 敏感内容检测）
    safe_query, is_safe, reject_reason = sanitize_input(req.query)
    if not is_safe:
        perf_logger.warning(f"[chat] blocked: reason={reject_reason} query_preview={req.query[:100]}")
        return ChatResponse(query=req.query, answer=safe_query, sources=[], is_casual=True)

    # F14：LLM 预算熔断前置检查——超限时不进入 Agent 流程，避免无谓的
    # 多轮调用尝试（LLM 是回答的必需品，故整体熔断而非局部降级）。
    budget_msg = _budget_block_message()
    if budget_msg:
        perf_logger.warning("[chat] budget_exceeded: llm")
        return ChatResponse(query=req.query, answer=budget_msg, sources=[], is_casual=True)

    try:
        if AGENT_ENABLED:
            agent = get_agent()
            result = agent.ask(safe_query, history=_sanitize_history(req.history), session_id=req.session_id)
            elapsed = (time.perf_counter() - t_start) * 1000
            # F12 v1（D-M3-9a）：B 类场景需人工确认 → 返回确认载荷，不给 answer
            confirmation = result.get("confirmation_required")
            if confirmation:
                perf_logger.info(f"[chat] confirmation_required scene={confirmation['scene']}")
                return ChatResponse(query=req.query, answer="", sources=[], confirmation=confirmation)
            ret_docs = result.get("retrieved_docs", [])
            # M2 / F10：优先用融合后的 fused_sources（去重 + 来源加权排序 + verification
            # 验证状态标注），与流式路径行为一致；融合不可用时回退原始检索结果。
            fused = result.get("fused_sources")
            perf_logger.info(
                f"[chat] mode=agent query_len={len(req.query)} "
                f"legal={result.get('is_legal_query', True)} "
                f"retrieved={len(ret_docs)} fused={len(fused or [])} elapsed={elapsed:.0f}ms"
            )
            return ChatResponse.from_rag_answer(
                query=result["query"],
                answer=result["answer"],
                sources=_dicts_to_retrieved(fused if fused is not None else ret_docs),
                is_casual=not result.get("is_legal_query", True),
            )

        engine = get_engine()
        history = _dicts_to_messages(_sanitize_history(req.history))

        t_route = time.perf_counter()
        if not needs_retrieval(req.query, engine.llm):
            # 能力问句("你能做什么") → 固定能力清单，不调 LLM（避免编造系统不具备的能力）
            if is_capability_query(req.query):
                answer = get_capability_reply()
            else:
                answer = engine.llm.chat(req.query, history=history)
            elapsed = (time.perf_counter() - t_start) * 1000
            perf_logger.info(
                f"[chat] mode=casual query_len={len(req.query)} "
                f"route_ms={(time.perf_counter() - t_route) * 1000:.0f} elapsed={elapsed:.0f}ms"
            )
            return ChatResponse.from_rag_answer(query=req.query, answer=answer, sources=[], is_casual=True)

        t_ret = time.perf_counter()
        docs = engine.retriever.search(req.query, top_k=req.top_k)
        ret_ms = (time.perf_counter() - t_ret) * 1000

        t_llm = time.perf_counter()
        prompt = engine._build_prompt(req.query, docs)
        answer = engine.llm.chat(prompt, history=history)
        llm_ms = (time.perf_counter() - t_llm) * 1000

        elapsed = (time.perf_counter() - t_start) * 1000
        top_score = round(docs[0].score, 4) if docs else 0
        perf_logger.info(
            f"[chat] mode=rag query_len={len(req.query)} "
            f"retrieved={len(docs)} top_score={top_score} "
            f"ret_ms={ret_ms:.0f} llm_ms={llm_ms:.0f} elapsed={elapsed:.0f}ms"
        )
        return ChatResponse.from_rag_answer(query=req.query, answer=answer, sources=docs)
    except HTTPException:
        raise
    except Exception as e:
        elapsed = (time.perf_counter() - t_start) * 1000
        perf_logger.error(f"[chat] error={type(e).__name__}: {e} elapsed={elapsed:.0f}ms")
        raise HTTPException(status_code=500, detail="处理请求失败，请稍后重试")


def _sse(data: dict) -> str:
    """将 dict 序列化为 SSE 格式的一行"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _is_disconnected(request: Request) -> bool:
    """检测 SSE 客户端是否已断开（fastapi 提供 is_disconnected）"""
    try:
        return await request.is_disconnected()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 流式桥接：同步生成器 → 异步生成器
#
# LLM / 检索 / Agent 全是同步代码。若直接在 async 生成器里迭代，会阻塞整个
# 事件循环（一个慢请求拖垮所有请求），且无法在模型生成中途响应客户端断开，
# 导致用户取消后后端仍持续消耗 Token。
#
# 方案：
#   1. 同步生成器放到独立线程池中迭代，事件循环只负责收事件、检查断开、编码 SSE。
#   2. 检测到客户端断开时，从主协程 close() 底层同步生成器 → 触发 GeneratorExit
#      → LLM 后端的 finally 立即关闭 HTTP 连接，停止 Token 消耗。
# ---------------------------------------------------------------------------
_STREAM_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(8, LLM_MAX_CONCURRENCY * 2),
    thread_name_prefix="sse-stream",
)
# 并发流上限：防止过多请求同时打向供应商触发 429 / 本地显存溢出。
_STREAM_SEMAPHORE = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

_SENTINEL_END = ("__stream_end__", None)

# ---------------------------------------------------------------------------
# 主动取消：前端点击"停止"后除断开连接外，还会发送 /chat/cancel
# 设置取消标记。这覆盖了经反向代理（nginx / vite proxy）时断开信号
# 传不到后端、后端感知不到客户端断开的场景。
# ---------------------------------------------------------------------------
_CANCEL_FLAGS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()

# D-M3-12：仍在生成中的流式请求（request_id → 开始时刻）。
# worker 结束时自除；resume 接口据此判断「重放后是否需要跟进新事件」。
# 单进程部署（Dockerfile 现状）下进程内注册表即可；重启后日志仍在 Redis，
# 重连重放完即止（生成已不在进行）。
_ACTIVE_STREAMS: dict[str, float] = {}

# 后台任务强引用集合（2026-09-01 审查整改）：asyncio 只持任务弱引用，
# fire-and-forget 的返回值丢弃后任务可能在完成前被 GC。add + 完成时 discard。
_BACKGROUND_TASKS: set = set()


def _spawn_background(coro) -> asyncio.Task:
    """创建后台任务并持强引用，完成时自动移除（防 GC + 防集合膨胀）。"""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


@router.post("/chat/cancel")
def cancel_chat(req: CancelRequest):
    """设置取消标记，对应的流式生成会在下一个事件周期立即中断"""
    with _CANCEL_LOCK:
        ev = _CANCEL_FLAGS.pop(req.request_id, None)
    if ev is not None:
        ev.set()
    return {"ok": True}


@router.post("/chat/confirm")
def confirm_chat(req: ConfirmRequest):
    """F12 v1 人工确认（D-M3-9a）：B 类场景的确认 / 取消标记。

    确认点在进图之前（此时未发生任何 LLM 调用），approved=True 写入确认标记
    （Redis TTL，默认 10 分钟），前端随后重新发起 /api/chat/stream（同
    session_id）即正常执行；approved=False 清除既有标记。

    场景必须是 F11 清单中的 B 类；存储失败返回 ok=False 供前端提示重试
    （确认机制故障不阻断主链路，见 ConfirmationStore 的 fail-open 语义）。
    """
    scene = get_scene(req.scene_id)
    if scene is None or scene.kind != KIND_B:
        raise HTTPException(400, f"非法确认场景: {req.scene_id}（仅接受 B 类场景 id）")
    store = get_confirmation_store()
    if not req.approved:
        store.clear("", req.session_id)
        return {"ok": True}
    ok = store.confirm("", req.session_id, req.query)
    if not ok:
        return JSONResponse(status_code=503, content={"ok": False, "message": "确认写入失败，请重试"})
    return {"ok": True}


@router.get("/chat/stream/resume")
async def resume_stream(
    request: Request,
    request_id: str = "",
    after_seq: int = 0,
    _user: str = Depends(require_registered_user),
):
    """D-M3-12 断线重连：重放 after_seq 之后的事件，再跟进新事件直到终局。

    鉴权说明（2026-09-01 审查整改）：重放的是用户自己的提问/回答全文，匿名可
    枚举 request_id 拉取他人会话内容，必须登录。前端走 fetch 重连（非
    EventSource），可正常携带 Authorization 头。

    事件来自 Redis/内存日志（`lexagent:stream:{request_id}:events`，TTL 默认
    10 分钟）。生成仍在进行（_ACTIVE_STREAMS 有登记）时轮询日志跟进新事件；
    读到 `__stream_end__` 终局标记、生成已不在进行、连接再次断开或超过兜底
    时限即结束。前端收到的事件与首次流完全一致（含 seq 游标）。
    """
    if not request_id:
        raise HTTPException(400, "request_id 必填")
    log = get_stream_log()
    if not log.exists(request_id):
        raise HTTPException(404, "无此流的事件日志（已过期或该请求未启用重连）")

    async def _gen():
        last = max(0, int(after_seq))
        # 兜底时限与日志 TTL 同量级：防止对端半开连接把协程挂死
        deadline = time.monotonic() + 600
        while True:
            ended = False
            for ev in log.read_after(request_id, last):
                last = int(ev.get("seq", last))
                if ev.get("type") == "__stream_end__":
                    ended = True
                    break
                yield _sse(ev)
            if ended or request_id not in _ACTIVE_STREAMS:
                break  # 终局；或生成已不在进行（取消/重启）——重放完即止
            if time.monotonic() > deadline:
                yield _sse({"type": "error", "content": "重连等待超时，请重新发起提问"})
                break
            if await _is_disconnected(request):
                break
            await asyncio.sleep(0.5)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _bridge_sync_stream(
    gen_factory: Callable[[], Iterator[dict]],
    request: Request,
    disconnect_event: asyncio.Event | None = None,
    cancel_event: threading.Event | None = None,
    stream_id: str = "",
):
    """在后台线程迭代同步生成器，主协程逐步产出事件。

    - 不阻塞事件循环

    事件日志（D-M3-12 断线重连）：stream_id 非空时，每个事件先写入
    StreamEventLog（带递增 seq）再投递在线队列——日志是重连补发的唯一真相源，
    在线消费者丢弃事件无妨。

    退出语义（D-M3-12 的关键设计）：
    - **主动取消**（cancel_event，/chat/cancel）：立即关闭底层生成器，停止
      LLM 消耗（现状不变）；
    - **被动断线**（disconnect_event / 连接关闭）：worker 与生成器**继续跑完**
      并持续写事件日志——LLM 成本已沉没，跑完重连用户才能补发完整事件流；
      在线协程立即停止产出，不再等待 worker。

    disconnect_event: 由调用方监听 ASGI http.disconnect 设置的事件。
      request.is_disconnected() 实现依赖 CancelScope 立即取消，在连接
      未断开时会抛 CancelledError 被吞掉返回 False；且会与 StreamingResponse
      内部监听竞争同一个 receive 通道。因此以事件监听为主、is_disconnected 兜底。

    cancel_event: 前端点击停止后通过 /chat/cancel 设置的取消标记，
      用于覆盖经反向代理时断开信号传不到后端的场景。
    """
    event_log = get_stream_log() if stream_id else None

    async def _client_gone() -> bool:
        if disconnect_event is not None and disconnect_event.is_set():
            return True
        if cancel_event is not None and cancel_event.is_set():
            return True
        return await _is_disconnected(request)

    q: _queue.Queue = _queue.Queue(maxsize=64)
    stop = threading.Event()
    loop = asyncio.get_running_loop()
    gen_ref: dict = {"gen": None}
    finished_normally = False

    def _run() -> None:
        if stream_id:
            _ACTIVE_STREAMS[stream_id] = time.monotonic()
        gen = None
        try:
            gen = gen_factory()
            gen_ref["gen"] = gen
            for item in gen:
                if stop.is_set():
                    # 主动取消：在当前（生成器执行）线程 close，GeneratorExit
                    # 立即在挂起的 yield 点投递，底层 LLM 流的 finally
                    # 立即关闭连接，停止 Token 消耗。
                    gen.close()
                    break
                # 先落日志再投递（D-M3-12）：写失败只告警，事件照常在线投递
                if event_log is not None:
                    try:
                        item = {**item, "seq": event_log.append(stream_id, item)}
                    except Exception as e:
                        logger.warning(f"事件日志写入失败（不影响在线流）: {e}")
                try:
                    q.put(item, timeout=1.0)
                except _queue.Full:
                    # 消费者不再取事件：被动断线下丢弃在线投递、继续跑完写日志；
                    # 主动取消则退出。
                    if stop.is_set():
                        gen.close()
                        break
                    continue
        except GeneratorExit:
            pass
        except Exception as e:
            if event_log is not None:
                event_log.append(stream_id, {"type": "error", "content": "处理失败，请稍后重试"})
            try:
                q.put(("__stream_error__", e), timeout=1.0)
            except _queue.Full:
                pass
        finally:
            if event_log is not None:
                # 终局标记：重连方读到即知流已结束（含取消场景），不会无限等待
                event_log.append_end(stream_id)
            if stream_id:
                _ACTIVE_STREAMS.pop(stream_id, None)
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass
            try:
                q.put(_SENTINEL_END, timeout=1.0)
            except _queue.Full:
                pass

    worker = loop.run_in_executor(_STREAM_EXECUTOR, _run)

    try:
        while True:
            # 从后台线程取事件（至多阻塞 1s，便于轮询断开状态）
            try:
                item = await asyncio.to_thread(q.get, True, 1.0)
            except _queue.Empty:
                if await _client_gone():
                    _on_exit_gone(stop, cancel_event, stream_id)
                    break
                continue

            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__stream_end__":
                finished_normally = True
                break
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__stream_error__":
                finished_normally = True  # 线程已结束并清理
                raise item[1]

            # 产出前检查断开
            if await _client_gone():
                _on_exit_gone(stop, cancel_event, stream_id)
                break  # 被动断线（有日志）：worker 继续跑完写日志（见 finally）
            yield item
    finally:
        # 终局语义（D-M3-12）：只有「带日志的被动断线」才让 worker 跑完补发；
        # 主动取消、无日志（未带 request_id）一律立即停，省 Token。
        persist = bool(stream_id) and not finished_normally and not stop.is_set()
        if not persist:
            stop.set()
            gen = gen_ref.get("gen")
            if gen is not None:
                try:
                    # 跨线程 close：CPython 允许，GeneratorExit 在生成器线程抛出，
                    # 底层 LLM 流 finally 关闭 HTTP 连接
                    await asyncio.to_thread(gen.close)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(worker, timeout=5.0)
            except Exception:
                pass  # 线程仍在清理时忽略，worker 最终会被线程池回收
        # persist：不杀 worker、不等待——它后台跑完持续写日志，供重连补发


def _on_exit_gone(stop: threading.Event, cancel_event: threading.Event | None, stream_id: str) -> None:
    """在线协程检测到客户端离开时的处置（D-M3-12）。

    - 主动取消（/chat/cancel）→ 置 stop：worker 立即停，省 Token（现状不变）；
    - 被动断线但**无日志**（前端未带 request_id）→ 置 stop：无人能重连补发，
      保持旧行为立即停；
    - 被动断线且有日志 → 什么都不做：worker 继续跑完写日志，重连可补发。
    """
    if cancel_event is not None and cancel_event.is_set():
        stop.set()
    elif not stream_id:
        stop.set()


def _iter_engine_stream(engine, query: str, history: list) -> Iterator[dict]:
    """非 Agent 路径：RAG 问答同步生成器（含意图识别 / 检索 / 流式生成）。

    由 _bridge_sync_stream 放到后台线程执行。
    """
    yield {"type": "thinking", "content": "正在分析问题..."}
    casual = not needs_retrieval(query, engine.llm)
    yield {"type": "thinking", "content": f"意图识别: {'闲聊 → 直接回复' if casual else '法律问题 → 检索法条'}"}

    if casual:
        yield {"type": "meta", "sources": [], "is_casual": True}
        yield {"type": "thinking", "content": "直接回复，无需检索"}
        # 能力问句("你能做什么") → 固定能力清单，不调 LLM（避免编造系统不具备的能力）
        if is_capability_query(query):
            yield {"type": "token", "content": get_capability_reply()}
            yield {"type": "thinking", "content": "完成"}
            return
        for token in engine.llm.chat_stream(query, history=history):
            yield {"type": "token", "content": token}
        yield {"type": "thinking", "content": "完成"}
        return

    t_ret = time.perf_counter()
    yield {"type": "thinking", "content": "正在检索法律条文..."}
    docs = engine.retriever.search(query, top_k=engine.top_k)
    ret_ms = (time.perf_counter() - t_ret) * 1000
    prompt = engine._build_prompt(query, docs)
    top_score = round(docs[0].score, 4) if docs else 0
    perf_logger.info(f"[stream] mode=rag retrieved={len(docs)} top_score={top_score} ret_ms={ret_ms:.0f}ms")
    yield {"type": "thinking", "content": f"检索完成，找到 {len(docs)} 条相关条文"}
    if docs:
        citations = [f"{d.law_name} {d.article_range}" for d in docs[:5]]
        yield {"type": "thinking", "content": f"引用: {', '.join(citations)}"}

    sources = [
        {
            "law_name": s.law_name,
            "chapter": s.chapter,
            "article_range": s.article_range,
            "citation": s.citation,
            "score": float(s.score),
            "content": s.content,
        }
        for s in docs
    ]
    yield {"type": "meta", "sources": sources, "is_casual": False}
    yield {"type": "thinking", "content": "模型正在生成回答..."}
    for token in engine.llm.chat_stream(prompt, history=history):
        yield {"type": "token", "content": token}


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    t_start = time.perf_counter()

    # 输入安全过滤（Prompt 注入 + 敏感内容检测）
    safe_query, is_safe, reject_reason = sanitize_input(req.query)
    if not is_safe:

        async def _reject_stream():
            yield _sse({"type": "error", "content": safe_query})
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _reject_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # F14：LLM 预算熔断前置检查（与非流式 /api/chat 口径一致）
    budget_msg = _budget_block_message()
    if budget_msg:
        perf_logger.warning("[stream] budget_exceeded: llm")

        async def _budget_stream():
            yield _sse({"type": "thinking", "content": "⚠️ 系统今日 AI 服务额度已用尽"})
            yield _sse({"type": "token", "content": budget_msg})
            yield _sse({"type": "meta", "sources": [], "is_casual": True})
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _budget_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if AGENT_ENABLED:
        agent = get_agent()
        safe_history = _sanitize_history(req.history)

        def _agent_gen() -> Iterator[dict]:
            # SSE 桥接已通用透传任意 dict 事件；此处仅对 tool_result 失败做日志埋点（F4）
            for event in agent.stream(safe_query, history=safe_history, session_id=req.session_id):
                if event.get("type") == "tool_result" and not event.get("ok", True):
                    perf_logger.warning(
                        f"[stream] tool_result failed: tool={event.get('tool')} "
                        f"summary={event.get('summary')} turn={event.get('turn')}"
                    )
                yield event

        gen_factory: Callable[[], Iterator[dict]] = _agent_gen
        mode = "agent"
    else:
        engine = get_engine()
        history = _dicts_to_messages(_sanitize_history(req.history))

        def _rag_gen() -> Iterator[dict]:
            return _iter_engine_stream(engine, safe_query, history)

        gen_factory: Callable[[], Iterator[dict]] = _rag_gen
        mode = "rag"

    # 可靠的断开检测：直接监听 ASGI http.disconnect 事件。
    # （uvicorn 在连接断开后会持续返回 http.disconnect，不受
    #   StreamingResponse 内部监听竞争影响；request.is_disconnected()
    #   仅在桥接中作兜底。）
    disconnect_event = asyncio.Event()

    # 主动取消标记：前端停止时经 /chat/cancel 设置，覆盖代理场景
    cancel_event: threading.Event | None = None
    if req.request_id:
        cancel_event = threading.Event()
        with _CANCEL_LOCK:
            _CANCEL_FLAGS[req.request_id] = cancel_event

    async def _listen_disconnect() -> None:
        try:
            while True:
                msg = await request.receive()
                if msg["type"] == "http.disconnect":
                    disconnect_event.set()
                    return
        except Exception:
            pass  # 连接已关闭或异常，视为已断开

    async def generate():
        try:
            # 并发上限：排队等待，避免打爆供应商 / 显存
            async with _STREAM_SEMAPHORE:
                async for event in _bridge_sync_stream(
                    gen_factory, request, disconnect_event, cancel_event, stream_id=req.request_id
                ):
                    yield _sse(event)
        except Exception as e:
            elapsed = (time.perf_counter() - t_start) * 1000
            perf_logger.error(f"[stream] mode={mode} error={type(e).__name__}: {e} elapsed={elapsed:.0f}ms")
            yield _sse({"type": "error", "content": "处理失败，请稍后重试"})
        finally:
            yield "data: [DONE]\n\n"

    agen = generate()

    async def _watchdog() -> None:
        """客户端断开 / 主动取消后主动关闭生成器。

        客户端断开时 StreamingResponse 会停止迭代 generate()，generate()
        会挂死在 yield 处，桥接的清理 finally（close 底层 LLM）不会执行，
        Token 继续被消耗。watchdog 检测到断开/取消后立即 aclose 生成器，
        使清理逻辑可靠执行。
        """
        try:
            while True:
                if disconnect_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
                    await agen.aclose()
                    return
                await asyncio.sleep(0.2)
        except Exception:
            pass

    listener = asyncio.create_task(_listen_disconnect())
    watchdog = asyncio.create_task(_watchdog())

    async def _finalize() -> None:
        # 释放取消标记，防止泄漏
        if req.request_id:
            with _CANCEL_LOCK:
                _CANCEL_FLAGS.pop(req.request_id, None)
        listener.cancel()
        watchdog.cancel()
        for t in (listener, watchdog):
            try:
                await t
            except Exception:
                pass

    response = StreamingResponse(
        agen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    # 响应结束后（含客户端断开）执行收尾，取消监听/看门狗任务
    response.background = BackgroundTask(_finalize)
    return response


# ------------------------------------------------------------------
# 对话持久化（全部按 user_id 隔离）
# ------------------------------------------------------------------


@router.get("/conversations")
def list_conversations(user_id: str = Depends(get_current_user)):
    """列出当前用户的对话会话"""
    from .conversation_store import get_conversation_store

    store = get_conversation_store()
    return store.list_sessions(user_id=user_id)


@router.get("/conversations/{session_id}")
def get_conversation(session_id: str, user_id: str = Depends(get_current_user)):
    """加载指定会话的对话历史（仅限当前用户）"""
    from .conversation_store import get_conversation_store

    store = get_conversation_store()
    history = store.load_history(user_id=user_id, session_id=session_id)
    return {"session_id": session_id, "history": history}


# 会话保存上限：防超大 JSON body 打爆内存 / PG 磁盘
_SAVE_MAX_MESSAGES = 500
_SAVE_MAX_BYTES = 2 * 1024 * 1024  # 2MB


def _persist_memory_background(user_id: str, session_id: str, messages: list[dict]) -> None:
    """后台异步固化对话记忆（Best-Effort，失败不影响主流程）。

    触发条件由 ConversationMemoryManager 内部判断（≥6 轮 + 幂等检查），
    这里只负责把完整会话交给记忆管理器。匿名用户跳过，
    避免把不同访客的对话混入同一份记忆。
    """
    try:
        from .dependencies import get_memory_manager
        from .auth import ANONYMOUS_USER_ID

        if not user_id or user_id == ANONYMOUS_USER_ID:
            return
        mgr = get_memory_manager()
        if mgr is None:
            logger.warning("记忆管理器未就绪，跳过记忆固化")
            return
        mgr.save_memory(user_id, session_id, messages)
    except Exception as e:
        logger.warning(f"后台记忆固化失败（可忽略）: {type(e).__name__}: {e}")


@router.post("/conversations/{session_id}")
def save_session(session_id: str, body: dict, user_id: str = Depends(get_current_user)):
    """保存整个会话的 JSON 消息数组（每次整体覆盖，不逐条插入）

    保存完成后若消息数达到记忆触发阈值，后台异步生成摘要固化长期记忆
    （幂等 UPSERT，同一会话重复保存不会产生重复记忆）。
    """
    from .conversation_store import get_conversation_store

    store = get_conversation_store()
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise HTTPException(400, "messages 必须为数组")
    if len(messages) > _SAVE_MAX_MESSAGES:
        raise HTTPException(400, f"消息条数过多: {len(messages)}（限制 {_SAVE_MAX_MESSAGES}）")
    payload_size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    if payload_size > _SAVE_MAX_BYTES:
        raise HTTPException(400, f"会话体积过大: {payload_size / 1024 / 1024:.1f}MB（限制 2MB）")
    store.save_session(user_id=user_id, session_id=session_id, messages=messages)

    # 异步触发记忆固化（≥6 轮才写，内部幂等）
    from src.memory.conversation import SUMMARY_TRIGGER_ROUNDS

    if len(messages) >= SUMMARY_TRIGGER_ROUNDS:
        resp = JSONResponse({"ok": True})
        resp.background = BackgroundTask(_persist_memory_background, user_id, session_id, messages)
        return resp
    return {"ok": True}


@router.delete("/conversations/{session_id}")
def delete_session(session_id: str, user_id: str = Depends(get_current_user)):
    """删除指定会话"""
    from .conversation_store import get_conversation_store

    store = get_conversation_store()
    store.delete_session(user_id=user_id, session_id=session_id)
    return {"ok": True}


# ------------------------------------------------------------------
# 认证路由
# ------------------------------------------------------------------


@auth_router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest):
    """注册新用户（需要用户名+密码），返回 Bearer Token"""
    return register_user(username=req.username, password=req.password)


@auth_router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest):
    """用用户名+密码登录，返回 Bearer Token"""
    return login_user(username=req.username, password=req.password)


@auth_router.get("/me")
def get_me(user_id: str = Depends(get_current_user)):
    """获取当前用户信息"""
    from .auth import ANONYMOUS_USER_ID

    is_anonymous = user_id == ANONYMOUS_USER_ID
    return {"user_id": user_id, "anonymous": is_anonymous}


# M2 / F10 引用溯源字段：融合结果（fused_sources）附带，固定管线检索结果没有。
# 动态构造兼容对象时必须一并保留，否则非流式 /api/chat 会丢失验证状态标注。
_SOURCE_TRACE_KEYS = ("source", "verification", "url", "law_status", "superseded")


def _dicts_to_retrieved(docs: list[dict]) -> list:
    """将 agent 返回的 dict 转为 RetrievedDoc 兼容格式（保留引用溯源字段）"""
    result = []
    for d in docs:
        attrs = {
            "law_name": d.get("law_name", ""),
            "chapter": d.get("chapter", ""),
            "section": d.get("section", ""),
            "article_range": d.get("article_range", ""),
            "citation": d.get("citation", ""),
            "content": d.get("content", ""),
            "score": float(d.get("score", 0)),
        }
        for key in _SOURCE_TRACE_KEYS:
            val = d.get(key)
            if val not in ("", None, False):
                attrs[key] = val
        result.append(type("RetrievedDoc", (), attrs)())
    return result


def _sources_with_content(sources: list) -> list[dict]:
    """将 RetrievedDoc/兼容对象序列化为含条文原文 content 的 sources 列表。

    供 agent 路径返回引用条文时带上原文，前端可折叠查看完整法条。
    """
    return [
        {
            "law_name": s.law_name,
            "chapter": s.chapter,
            "section": getattr(s, "section", ""),
            "article_range": s.article_range,
            "citation": s.citation,
            "score": float(s.score),
            "content": getattr(s, "content", ""),
        }
        for s in sources
    ]


# ---------------------------------------------------------------------------
# 4. 知识库 — 文档上传
# ---------------------------------------------------------------------------
# 解析管道单例（任务状态跨请求共享）
_ingestion_pipeline: object | None = None


def _get_ingestion_pipeline():
    """获取解析管道单例"""
    global _ingestion_pipeline
    if _ingestion_pipeline is None:
        embedder = _create_embedder()
        from src.knowledge.pgvector_store import PgvectorStore
        from src.config import PG_CONN as _pg_conn

        store = PgvectorStore(_pg_conn)
        store.ensure_tables()
        from src.knowledge.ingestion.pipeline import IngestionPipeline

        _ingestion_pipeline = IngestionPipeline(store, embedder)
    return _ingestion_pipeline


_UPLOAD_ALLOWED_STATUS = {"active", "repealed", "revised", "pending"}


@router.post("/knowledge/upload")
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form("law"),
    source: str = Form(""),
    effective_date: str = Form(""),
    status: str = Form("active"),
    _user: str = Depends(require_registered_user),
):
    """上传法律文档（PDF/DOCX/TXT）—— 需登录

    文件被保存到临时目录后由解析管道处理，
    返回 task_id 用于查询处理进度。

    （2026-09-01 审查整改：docstring 原来写在两次校验之后，成了永不生效的
    死字符串——docstring 必须是函数体第一条语句才会被识别。）
    """
    # 归一到规范 doc_type（兼容前端旧别名 interpretation/local/judicial）
    from src.knowledge.doc_types import normalize_doc_type

    doc_type = normalize_doc_type(doc_type)

    # 校验效力状态（防止伪造非法值）
    if status not in _UPLOAD_ALLOWED_STATUS:
        raise HTTPException(400, f"无效的效力状态: {status}，可选 {sorted(_UPLOAD_ALLOWED_STATUS)}")
    import tempfile
    import asyncio

    # 验证文件扩展名
    ext = Path(file.filename or "").suffix.lower()
    allowed = {".pdf", ".docx", ".txt"}
    if ext not in allowed:
        raise HTTPException(400, f"不支持的文件格式: {ext}，支持: {', '.join(allowed)}")

    # 检查文件大小 — 带上限读取，避免超大文件先占满内存再被拒
    max_size = 50 * 1024 * 1024  # 50MB
    content = await file.read(max_size + 1)
    if len(content) > max_size:
        raise HTTPException(400, "文件过大（限制 50MB）")

    # 保存到临时文件
    suffix = ext if ext else ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    # 提交解析任务（透传效力状态）
    pipeline = _get_ingestion_pipeline()
    task_id = pipeline.submit(
        file_path=tmp_path,
        doc_type=doc_type,
        source=source,
        effective_date=effective_date or None,
        status=status,
    )

    # 后台异步处理 — to_thread 避免同步解析阻塞事件循环。
    # 持强引用（2026-09-01 审查整改）：asyncio 对任务只持弱引用，返回值丢弃后
    # 任务可能在完成前被 GC——文档解析静默消失、状态永远停在 pending。
    _spawn_background(asyncio.to_thread(_run_ingestion_sync, pipeline, task_id, tmp_path))

    return {
        "task_id": task_id,
        "filename": file.filename,
        "doc_type": doc_type,
        "status": "pending",
        "message": f"文档 {file.filename} 已提交解析",
    }


def _run_ingestion_sync(pipeline, task_id: str, tmp_path: str):
    """后台同步执行解析任务（运行在 asyncio.to_thread 线程中）"""
    import os

    try:
        chunk_count = pipeline.run(task_id)
        logger.info(f"后台解析完成: task={task_id[:8]}..., chunks={chunk_count}")
    except Exception as e:
        logger.error(f"后台解析失败: task={task_id[:8]}..., error={e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception as e:
            logger.debug(f"临时文件清理失败（可忽略）: {tmp_path}: {e}")


@router.get("/knowledge/status/{task_id}")
async def get_ingestion_status(task_id: str, _user: str = Depends(require_registered_user)):
    """查询文档解析任务状态 —— 需登录。

    鉴权说明（2026-09-01 审查整改）：上传接口本身已要求登录，其任务状态属于
    同一管理链路，匿名可枚举 task_id 探测他人上传内容。
    """
    pipeline = _get_ingestion_pipeline()
    status = pipeline.get_status(task_id)
    if status is None:
        raise HTTPException(404, "任务不存在")
    return status


# ---------------------------------------------------------------------------
# 5. 知识库 — 文档管理
# ---------------------------------------------------------------------------


_STORE_SINGLETON = None  # PgvectorStore | None（避免仅为注解引入导入开销）
_STORE_LOCK = threading.Lock()


def _get_store():
    """获取 pgvector store 进程级单例（2026-09-01 审查整改）。

    原来：每次调用 new 一个 `PgvectorStore`（每请求 `psycopg2.connect` 新建
    PG 连接）且从不关闭——并发下连接数随请求线性增长，直至 PG
    max_connections 耗尽；`ensure_tables()` 的存在性查询也每请求白跑一次。
    现在：模块级单例 + 双检锁。连接自身有 `_ensure_connection()` 断线自动
    重连（`_locked` 串行化保护共享连接），单例不会被断连拖死。
    """
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        with _STORE_LOCK:
            if _STORE_SINGLETON is None:
                from src.knowledge.pgvector_store import PgvectorStore
                from src.config import PG_CONN as _pg_conn

                store = PgvectorStore(_pg_conn)
                store.ensure_tables()
                _STORE_SINGLETON = store
    return _STORE_SINGLETON


def close_store() -> None:
    """应用关闭时释放知识库 PG 连接（main.lifespan 的 finally 调用）。

    关闭失败不抛出——进程退出路径上的清理不应反过来制造新故障。
    """
    global _STORE_SINGLETON
    with _STORE_LOCK:
        if _STORE_SINGLETON is not None:
            try:
                _STORE_SINGLETON.close()
            except Exception as e:
                logger.debug(f"关闭知识库 PG 连接失败（可忽略）: {e}")
            finally:
                _STORE_SINGLETON = None


@router.get("/knowledge/documents")
def list_knowledge_documents(
    doc_type: str | None = None,
    status: str | None = None,
    q: str = "",
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 20,
    offset: int = 0,
    _user: str = Depends(require_registered_user),
):
    """列出知识库中的文档（分页 + 排序 + 关键词搜索）—— 需登录。

    鉴权说明（2026-09-01 审查整改）：原无任何 Depends，匿名可分页遍历下载
    整个知识库正文。与同一资源的写接口（upload / delete）口径对齐，改为硬鉴权。

    Query:
        doc_type: 按类型过滤（flk 顶级分类规范值），不传则返回全部
        status: 按效力状态过滤（active/repealed/revised/pending，可逗号组合），
                不传则返回全部（含废止/未生效，便于辨别法律效力）
        q: 关键词，同时匹配标题与正文内容（大小写不敏感）
        sort: 排序字段（created_at/updated_at/title/doc_type）
        order: asc / desc
        limit: 每页条数（默认 20，最大 200）
        offset: 跳过条数（配合前端无限滚动分页）

    Returns:
        {documents, total, limit, offset}
    """
    from src.knowledge.doc_types import normalize_doc_type

    store = _get_store()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    docs, total = store.list_documents(
        doc_type=normalize_doc_type(doc_type),
        status=status,
        q=q or None,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    return {"documents": docs, "total": total, "limit": limit, "offset": offset}


@router.delete("/knowledge/documents/{doc_id}")
def delete_knowledge_document(doc_id: str, _user: str = Depends(require_registered_user)):
    """删除文档及其所有向量块 —— 需登录"""
    store = _get_store()
    ok = store.delete_document(doc_id)
    if not ok:
        raise HTTPException(404, "文档不存在")
    # 注：HNSW 对删除是软处理（标记删除），无需全量 REINDEX（锁表）；
    # 大量删除后的索引整理由 rebuild 接口显式触发
    return {"ok": True, "message": f"文档 {doc_id[:8]}... 已删除"}


@router.get("/knowledge/documents/{doc_id}/chunks")
def get_document_chunks(
    doc_id: str,
    limit: int = 50,
    offset: int = 0,
    _user: str = Depends(require_registered_user),
):
    """获取文档的文本块（分页，默认每页 50 条）—— 需登录。

    鉴权说明（2026-09-01 审查整改）：与列表接口同源，匿名可拉全文正文。

    Query:
        limit: 每页条数（默认 50，最大 500）
        offset: 跳过条数（配合前端滚动懒加载）
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    store = _get_store()
    total = store.count_document_chunks(doc_id)
    if total == 0:
        raise HTTPException(404, "文档不存在或无内容")
    chunks = store.get_document_chunks(doc_id, limit=limit, offset=offset)
    return {"doc_id": doc_id, "chunks": chunks, "total": total, "limit": limit, "offset": offset}


# ----------------------------------------------------------------------------
# 6. 爬虫 — 国家法律法规数据库增量爬取
# ----------------------------------------------------------------------------
_crawl_tasks: dict[str, dict] = {}


@router.post("/crawl", response_model=CrawlTaskResponse)
async def crawl_laws(req: CrawlRequest, _user: str = Depends(require_registered_user)):
    """触发爬取（后台任务）—— 需登录。

    数据源现仅支持 npc（全国人大「国家法律法规数据库」）。任务提交后返回
    task_id，通过 GET /api/crawl/status/{task_id} 查询进度与结果。
    爬取的文档会落地到 LawData/<子目录>/ 并做增量去重。
    """
    if req.source != "npc":
        raise HTTPException(400, "暂仅支持 source=npc（国家法律法规数据库）")
    task_id = uuid4().hex[:12]
    _crawl_tasks[task_id] = {
        "status": "pending",
        "progress": {"total": 0, "added": 0, "updated": 0, "skipped": 0, "failed": 0},
        "errors": [],
        "files": [],
        "finished": False,
        "result": None,
        "rebuild": None,
    }
    # 持强引用（2026-09-01 审查整改），理由见 upload 处注释：弱引用任务可能
    # 被提前 GC，爬虫任务静默消失、进度永远停在 pending。
    _spawn_background(asyncio.to_thread(_run_crawl, task_id, req))
    return CrawlTaskResponse(
        task_id=task_id,
        status="pending",
        message="爬取任务已提交，请用 GET /api/crawl/status/{task_id} 查询进度",
    )


def _run_crawl(task_id: str, req: CrawlRequest) -> None:
    from dataclasses import asdict

    from src.knowledge.crawler import NpcLawCrawler
    from src.knowledge.doc_types import normalize_doc_type

    state = _crawl_tasks.get(task_id)
    if state is None:
        return
    state["status"] = "running"
    try:
        crawler = NpcLawCrawler()

        def _on_progress(r) -> None:
            state["progress"] = {
                "total": r.total,
                "added": r.added,
                "updated": r.updated,
                "skipped": r.skipped,
                "failed": r.failed,
            }

        res = crawler.crawl(
            doc_type=normalize_doc_type(req.doc_type),
            keyword=req.keyword,
            limit=req.limit,
            force=req.force,
            subdir=req.subdir,
            store=req.store,
            progress_cb=_on_progress,
        )
        state["result"] = asdict(res)
        state["errors"] = res.errors
        state["files"] = res.files
        state["progress"] = {
            "total": res.total,
            "added": res.added,
            "updated": res.updated,
            "skipped": res.skipped,
            "failed": res.failed,
        }
        state["finished"] = True
        state["status"] = "done"
        if req.rebuild:
            _trigger_rebuild(task_id)
    except Exception as e:
        state["status"] = "error"
        state["errors"] = [str(e)]
        state["finished"] = True
        logger.error(f"[crawl] task {task_id} 失败: {e}")


def _trigger_rebuild(task_id: str) -> None:
    """纯 PG 语义：重建 pgvector 的 HNSW 索引。

    v0.6 移除 FAISS 后，`rebuild` 不再触发 scripts/build_index.py，
    改为对 pgvector 做一次全量 reindex（可选、低频操作）。
    """
    state = _crawl_tasks.get(task_id)
    if state is None:
        return
    state["rebuild"] = "running"
    try:
        from src.config import PG_CONN
        from src.knowledge.pgvector_store import PgvectorStore

        store = PgvectorStore(PG_CONN)
        store.reindex()
        state["rebuild"] = "done"
    except Exception as e:
        state["rebuild"] = f"error: {e}"
        logger.error(f"[crawl] 重建 pgvector 索引失败: {e}")


@router.get("/crawl/status/{task_id}", response_model=CrawlStatusResponse)
async def get_crawl_status(task_id: str, _user: str = Depends(require_registered_user)):
    """查询爬取任务状态与结果 —— 需登录（与 POST /api/crawl 口径对齐）。"""
    state = _crawl_tasks.get(task_id)
    if state is None:
        raise HTTPException(404, "任务不存在")
    return CrawlStatusResponse(
        task_id=task_id,
        status=state["status"],
        progress=state["progress"],
        errors=state["errors"],
        files=state["files"],
        finished=state["finished"],
        rebuild=state.get("rebuild"),
        result=state.get("result"),
    )


@router.get("/crawl/types")
async def list_crawl_types(_user: str = Depends(require_registered_user)):
    """列出支持的爬取类型与说明（分类对齐 flk 国家法律法规数据库顶级分类）—— 需登录"""
    from src.knowledge.doc_types import crawlable_types

    types = crawlable_types()
    types["auto"] = "自动分类（按关键词搜索，逐条按 flxz 自动判定归属）"
    types["all"] = "全部（依次爬取上述类型）"
    return {
        "source": "npc",
        "types": types,
        "unsupported": ["case（案例 / 裁判文书，该数据源不提供）"],
    }
