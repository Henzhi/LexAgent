"""
API 依赖注入。

管理 LLM、向量库、RAG 引擎 / Agent 等单例，所有可配参数从 src.config 读取。
v0.6: 纯 PG 架构，检索后端统一为 pgvector（已移除 FAISS）。
"""

from __future__ import annotations

import logging

from src.config import (
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TOP_P,
    LLM_MAX_TOKENS,
    LLM_BACKEND,
    LLM_MAX_RETRIES,
    OPENAI_MODEL,
    EMBED_MODEL,
    EMBED_BATCH_SIZE,
    EMBED_MAX_RETRIES,
    RETRIEVAL_TOP_K,
    RERANK_ENABLED,
    RERANK_MODEL,
    RERANK_RECALL_K,
    RERANK_TOP_K,
    AGENT_MAX_RETRIES,
    PG_CONN,
    ADJACENT_ENABLED,
    ADJACENT_WINDOW,
    HYBRID_ENABLED,
    HYBRID_RRF_K,
    HYBRID_BM25_WEIGHT,
    HYBRID_ALWAYS_ON,
    LAW_NAME_BOOST_ENABLED,
    LAW_NAME_BOOST,
    LAW_NAME_BOOST_TOP_LAWS,
    REWRITE_FUSION_ENABLED,
    REWRITE_FUSION_RECALL_K,
    REWRITE_FUSION_RRF_K,
    FAQ_CACHE_BACKEND,
    REDIS_URL,
)
from src.llm.adapter import LLMAdapter, EmbeddingAdapter
from src.llm.factory import create_llm_backend
from src.embedding.factory import create_embedding_backend
from src.rag.engine import RAGEngine
from src.rag.retriever import PgvectorStoreRetriever
from src.agents.graph import LawAgentGraph

logger = logging.getLogger(__name__)

_engine: RAGEngine | None = None
_agent: LawAgentGraph | None = None
_llm: object | None = None  # LLMAdapter，兼容旧 LawLLM 接口
_memory_mgr: object | None = None  # ConversationMemoryManager | None
_query_logger: object | None = None  # QueryLogger | None（可观测性）


def get_llm():
    """获取 LLM 实例（通过适配器兼容旧 API）

    根据 LLM_BACKEND 环境变量自动选择后端:
      ollama → OllamaBackend（本地）
      openai → OpenAICompatibleBackend（API，M1 默认）+ Ollama 降级（failover）

    base_url 等连接参数由工厂函数从环境变量读取，
    此处只传模型无关的通用参数（temperature/top_p/max_tokens）。

    M1（F5）：LLM_BACKEND=openai 时构建主备降级组合 FailoverLLMBackend；
    LLM_BACKEND=ollama 时保持原有单后端行为（AC-7）。
    """
    global _llm
    if _llm is None:
        # 按后端选择对应模型（避免 openai 后端误用 Ollama 模型名）
        if LLM_BACKEND in ("openai", "openai_compatible"):
            model = OPENAI_MODEL
        else:
            model = LLM_MODEL
        backend = create_llm_backend(
            backend_type=LLM_BACKEND,
            failover=LLM_BACKEND in ("openai", "openai_compatible"),
            model=model,
            temperature=LLM_TEMPERATURE,
            top_p=LLM_TOP_P,
            max_tokens=LLM_MAX_TOKENS,
            max_retries=LLM_MAX_RETRIES,
        )
        _llm = LLMAdapter(backend)
        degraded_note = "（降级模式）" if getattr(backend, "degraded", False) else ""
        logger.info(f"LLM 就绪: {LLM_BACKEND}:{model}{degraded_note} (context_window={backend.get_context_window()})")
    return _llm


def _create_embedder():
    """根据配置创建 Embedding 实例（通过适配器兼容旧 API）

    根据 EMBED_BACKEND 环境变量自动选择后端（默认 ollama，独立于 LLM_BACKEND）。
    base_url 等连接参数由工厂函数从环境变量读取，此处只传模型无关参数。
    """
    backend = create_embedding_backend(
        backend_type=None,  # 自动从环境变量读取
        model=EMBED_MODEL,
        batch_size=EMBED_BATCH_SIZE,
        max_retries=EMBED_MAX_RETRIES,
    )
    return EmbeddingAdapter(backend)


def _create_retriever(embedder):
    """创建检索器（纯 pgvector：PgvectorStore + halfvec + HNSW）

    v0.6 起强制 pgvector，不再支持 FAISS 回退。
    PG 连接失败将直接抛错（不静默降级），保证部署配置正确性。
    """
    from pathlib import Path

    from src.knowledge.pgvector_store import PgvectorStore

    logger.info("使用 pgvector 检索 (halfvec + HNSW)")
    store = PgvectorStore(PG_CONN)
    store.ensure_tables()
    retriever = PgvectorStoreRetriever(
        store=store,
        embedder=embedder,
        embedding_model=embedder.model,
    )

    # Reranker 精排（若启用）
    if RERANK_ENABLED:
        from src.rag.reranker import Reranker, RerankRetriever

        reranker = Reranker(model_name=RERANK_MODEL)
        retriever = RerankRetriever(
            base_retriever=retriever, reranker=reranker, recall_k=RERANK_RECALL_K, top_k=RERANK_TOP_K
        )
        logger.info(f"Reranker 就绪: 粗排{RERANK_RECALL_K} → 精排{RERANK_TOP_K}")

    # 法名推断软信号加权（B2 二阶段）：质心最近邻 top3 候选法名，仅无 法名查询激活。
    # 接入点在 Rerank 之后、Adjacent 之前——邻居扩展应跟随加权后的核心排序
    if LAW_NAME_BOOST_ENABLED:
        from src.rag.law_centroids import get_law_centroids
        from src.rag.law_name_boost import LawNameBoostRetriever

        retriever = LawNameBoostRetriever(
            base_retriever=retriever,
            embedder=embedder,
            centroids=get_law_centroids(),
            boost=LAW_NAME_BOOST,
            top_laws=LAW_NAME_BOOST_TOP_LAWS,
        )
        logger.info(f"法名加权就绪: top{LAW_NAME_BOOST_TOP_LAWS} 候选法名 boost={LAW_NAME_BOOST}")

    # 相邻扩展（article_map 缺失时自动降级为空转）
    if ADJACENT_ENABLED:
        from src.rag.adjacent_expander import AdjacentExpander

        map_path = Path(__file__).resolve().parents[2] / "data" / "vector_store" / "article_map.json"
        retriever = AdjacentExpander(base_retriever=retriever, article_map_path=map_path, window=ADJACENT_WINDOW)

    # BM25 关键词混合（rank-based 条件激活）：仅法名/条款查询参与，BM25 索引懒加载
    if HYBRID_ENABLED:
        from src.rag.bm25_retriever import Bm25Retriever
        from src.rag.hybrid_retriever import HybridRetriever

        bm25 = Bm25Retriever(store)
        retriever = HybridRetriever(
            base_retriever=retriever,
            bm25_retriever=bm25,
            rrf_k=HYBRID_RRF_K,
            bm25_weight=HYBRID_BM25_WEIGHT,
            always_on=HYBRID_ALWAYS_ON,
        )
        logger.info(
            f"BM25 混合就绪: RRF k={HYBRID_RRF_K}, bm25_w={HYBRID_BM25_WEIGHT}, "
            f"模式={'常开' if HYBRID_ALWAYS_ON else '条件激活'} (BM25 索引懒加载)"
        )

    # 条款号精确路由（最外层）：对"法名+第X条"查询做精确置顶，弥补纯向量对条款号查询的失配
    from src.rag.article_router import ArticleRouter

    retriever = ArticleRouter(base_retriever=retriever, store=store)

    # LLM 查询改写 + 双路 RRF 融合（最外层）：无法名口语查询的正交信号。
    # 两条路径都经过完整链（含 ArticleRouter/Hybrid），融合在最终排序上做
    if REWRITE_FUSION_ENABLED:
        from src.rag.law_centroids import get_law_centroids
        from src.rag.rewrite_fusion import RewriteFusionRetriever, make_default_llm

        retriever = RewriteFusionRetriever(
            base_retriever=retriever,
            llm=make_default_llm(),
            centroids=get_law_centroids(),
            recall_k=REWRITE_FUSION_RECALL_K,
            rrf_k=REWRITE_FUSION_RRF_K,
        )
        logger.info(
            f"改写融合就绪: 每路 recall_k={REWRITE_FUSION_RECALL_K}, RRF k={REWRITE_FUSION_RRF_K}"
        )

    return retriever


def get_engine() -> RAGEngine:
    global _engine
    if _engine is None:
        llm = get_llm()
        embedder = _create_embedder()
        retriever = _create_retriever(embedder)
        _engine = RAGEngine(
            retriever=retriever,
            llm=llm,
            top_k=RETRIEVAL_TOP_K,
            query_logger=get_query_logger(),
        )
        logger.info("RAG 引擎就绪")
    return _engine


def _create_memory_manager(llm, embedder):
    """创建对话记忆管理器（纯 PG，需要 pgvector 环境）"""
    try:
        from src.memory.conversation import ConversationMemoryManager

        return ConversationMemoryManager(conn_string=PG_CONN, embedder=embedder, llm=llm)
    except Exception as e:
        logger.warning(f"记忆管理器初始化失败（pgvector 未就绪？）: {e}")
        return None


def get_query_logger():
    """获取检索质量日志记录器单例（可观测性，失败不影响主流程）"""
    global _query_logger
    if _query_logger is None:
        try:
            from src.observability.query_log import QueryLogger

            _query_logger = QueryLogger(conn_string=PG_CONN)
        except Exception as e:
            logger.warning(f"QueryLogger 初始化失败（query_logs 表未建？）: {e}")
            _query_logger = None
    return _query_logger


def _create_faq_cache(embedder):
    """创建 FAQ 语义缓存管理器

    按 FAQ_CACHE_BACKEND 选择后端：
      - redis（默认）：Redis Stack，向量检索 + 原生 TTL，无需后台清理
      - pg：pgvector，定时清理（无 Redis 环境的回退方案）
    """
    try:
        if FAQ_CACHE_BACKEND == "pg":
            from src.memory.faq_cache import FAQCache

            logger.info("FAQ 缓存后端: pgvector")
            return FAQCache(conn_string=PG_CONN, embedder=embedder)

        from src.memory.faq_cache_redis import FAQCacheRedis

        cache = FAQCacheRedis(redis_url=REDIS_URL, embedder=embedder)
        cache.ensure_index()
        logger.info(f"FAQ 缓存后端: Redis Stack ({REDIS_URL})")
        return cache
    except Exception as e:
        logger.warning(f"FAQ缓存初始化失败: {e}")
        return None


def get_memory_manager():
    """获取对话记忆管理器单例（会话保存时异步固化记忆用）。

    与 get_agent 内创建的记忆管理器复用同一实例，避免重复连接 PG。
    """
    global _memory_mgr
    if _memory_mgr is None:
        llm = get_llm()
        embedder = _create_embedder()
        _memory_mgr = _create_memory_manager(llm, embedder)
    return _memory_mgr


def get_agent(force_reload: bool = False) -> LawAgentGraph:
    """获取 LangGraph 多 Agent 引擎（含记忆管理器 + FAQ 缓存 + M1 工具注册表）"""
    global _agent
    if force_reload:
        _agent = None
    if _agent is None:
        llm = get_llm()
        embedder = _create_embedder()
        retriever = _create_retriever(embedder)
        memory_mgr = get_memory_manager()
        faq_cache = _create_faq_cache(embedder)
        # M1（F1）：构建默认工具注册表（retrieve_knowledge + web_search）
        from src.agents.tools import build_default_tools

        registry = build_default_tools(retriever)
        _agent = LawAgentGraph(
            retriever=retriever,
            llm=llm,
            top_k=RETRIEVAL_TOP_K,
            max_retries=AGENT_MAX_RETRIES,
            memory_manager=memory_mgr,
            faq_cache=faq_cache,
            query_logger=get_query_logger(),
            registry=registry,
        )
        extras = []
        if memory_mgr:
            extras.append("记忆")
        if faq_cache:
            extras.append("FAQ缓存")
        if get_query_logger():
            extras.append("可观测性")
        logger.info(f"LangGraph Agent 就绪 ({'/'.join(extras) if extras else '基础模式'})")
    return _agent
