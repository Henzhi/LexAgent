"""
对话记忆管理器 (v0.5)

实现跨会话的记忆存储和检索:
  - 摘要生成: 对话 > 6 轮时，LLM 自动生成结构化摘要 + 关键实体提取
  - 存储: 摘要向量写入 pgvector conversation_memories 表，TTL 30 天
  - 检索: 新问题时语义检索 Top-3 历史摘要，时间衰减排序
  - 注入: 结果拼入 Prompt [历史参考] 段，让 LLM 带着记忆回答

用法:
    mgr = ConversationMemoryManager(store, embedder, llm)
    mgr.save_memory(user_id, session_id, messages)   # 对话结束时调用
    summaries = mgr.retrieve(user_id, query)          # 新问题时调用
"""
from __future__ import annotations

import json
import logging
import threading
from functools import wraps

import psycopg2
from pgvector.psycopg2 import register_vector

logger = logging.getLogger(__name__)


def _locked(method):
    """串行化对共享 PG 连接的访问（psycopg2 连接非线程安全）。

    流式桥接改造后，记忆检索可能在多个请求的线程池 worker 中并发执行，
    必须保护共享连接。
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_lock", None)
        if lock is None:
            # 防御：兼容绕过 __init__ 的构造方式（如测试 mock）
            lock = threading.Lock()
            self._lock = lock
        with lock:
            return method(self, *args, **kwargs)
    return wrapper

# 摘要生成 Prompt
_SUMMARY_PROMPT = """请将以下法律咨询对话总结为一段结构化摘要，用于后续记忆检索。

## 对话内容
{conversation}

## 摘要格式（严格按此结构输出，每个字段一行）
案件类型: <继承纠纷/合同纠纷/劳动争议/刑事/婚姻/行政/其他>
涉及法律: <列举涉及的法律名称，逗号分隔>
关键事实: <用户描述的核心案情，1-2句话>
已回答问题: <系统已经回答了什么>
未解决问题: <还有什么待回答>
"""

# 触发条件：对话轮数阈值
SUMMARY_TRIGGER_ROUNDS = 6

# 检索参数
DEFAULT_TOP_K = 3
TIME_DECAY_DAYS = 7  # 半衰期：记忆权重每 7 天衰减一半（指数衰减 e^{-λt}, λ = ln2/7）

# importance 预筛（写入链路规则预筛，避免低价值对话产生噪音记忆）：
# 简单规则分档，后续可用离线任务做更细粒度的冲突检测与合并。
IMPORTANCE_LOW_MSGS = 6      # ≥6 轮（触发线）：基础重要度
IMPORTANCE_MID_MSGS = 10     # ≥10 轮：中
IMPORTANCE_HIGH_MSGS = 15    # ≥15 轮：高

# 记忆 TTL（天）：与 init.sql 默认值保持一致
MEMORY_TTL_DAYS = 30


class ConversationMemoryManager:
    """对话记忆管理器

    独立管理自己的 PG 连接，不依赖 PgvectorStore 的内部实现。
    """

    def __init__(
        self,
        conn_string: str,
        embedder=None,  # EmbeddingAdapter | None（清理路径可传 None）
        llm=None,       # LLMAdapter | None（清理路径可传 None）
    ):
        """记忆管理器。

        Args:
            conn_string: PG 连接串
            embedder: 用于摘要向量化；仅执行 clean_expired 的清理场景可为 None
            llm: 用于摘要生成；仅执行 clean_expired 的清理场景可为 None
        """
        self._embedder = embedder
        self._llm = llm
        # 保存原始连接串用于重连 — conn.dsn 不保证回传密码，重连可能失败
        self._conn_string = conn_string
        self._conn = psycopg2.connect(conn_string)
        register_vector(self._conn)
        # schema 迁移状态（importance 列 + 幂等唯一索引），连接重建后需重跑
        self._schema_ready = False

    def _ensure_schema(self):
        """幂等 schema 迁移：旧库补 importance 列 + (user_id, session_id) 唯一索引。

        唯一索引是 save_memory 幂等写入（UPSERT）的前提；
        importance 列用于检索时的重要度加权。表不存在时忽略（init.sql 会建表）。
        """
        if self._schema_ready:
            return
        self._ensure_connection()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE conversation_memories "
                    "ADD COLUMN IF NOT EXISTS importance REAL DEFAULT 0.6"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_user_session "
                    "ON conversation_memories(user_id, session_id)"
                )
            self._conn.commit()
            self._schema_ready = True
        except Exception as e:
            logger.warning(f"记忆表 schema 迁移失败（表未创建？）: {e}")
            self._conn.rollback()
            self._schema_ready = False

    def _ensure_connection(self):
        """检查连接是否存活，断开则自动重连"""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            logger.warning("记忆管理器: PG 连接已断开，尝试重连...")
            try:
                self._conn.close()
            except Exception as close_e:
                logger.debug(f"记忆管理器 关闭旧连接失败（可忽略）: {close_e}")
            self._conn = psycopg2.connect(self._conn_string)
            register_vector(self._conn)
            logger.info("记忆管理器: PG 重连成功")

    @_locked
    def close(self):
        """关闭数据库连接"""
        self._conn.close()

    @_locked
    def clean_expired(self) -> int:
        """清理过期的对话记忆（expires_at < NOW()）。

        检索时已通过 WHERE expires_at > NOW() 过滤过期行，此处负责真正
        删除累积的过期记录，避免表无限膨胀。由后台定时任务周期调用。
        """
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversation_memories WHERE expires_at < NOW()"
            )
            count = cur.rowcount
        self._conn.commit()
        if count:
            logger.info(f"对话记忆清理: {count} 条过期")
        return count

    # ------------------------------------------------------------------
    # 记忆写入
    # ------------------------------------------------------------------

    def should_summarize(self, message_count: int) -> bool:
        """判断是否需要生成摘要（>6 轮时触发）"""
        return message_count >= SUMMARY_TRIGGER_ROUNDS

    @staticmethod
    def _estimate_importance(message_count: int) -> float:
        """根据对话轮数估算重要度（写入链路的规则预筛）

        轮数越多说明对话越深入、信息价值越高；简单分档避免每轮
        都跑 LLM 打分（噪音大 + 成本高），后续可离线做冲突检测/合并。

        Returns:
            0.6 / 0.8 / 1.0 三档
        """
        if message_count >= IMPORTANCE_HIGH_MSGS:
            return 1.0
        if message_count >= IMPORTANCE_MID_MSGS:
            return 0.8
        return 0.6

    @_locked
    def save_memory(
        self,
        user_id: str,
        session_id: str,
        messages: list[dict],
    ) -> str | None:
        """保存对话记忆（幂等 + 重要度预筛）

        1. 检查是否达到触发条件（≥6 轮）
        2. 幂等检查：同会话已存摘要且轮数未增长则跳过（避免重复写入噪音）
        3. LLM 生成结构化摘要
        4. 提取关键实体 + 按轮数分档重要度
        5. embedding → UPSERT 写入 pgvector（ON CONFLICT 覆盖巩固）

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            messages: 完整对话消息 [{"role": ..., "content": ...}, ...]

        Returns:
            摘要文本（用于日志），未触发/已存在则返回 None
        """
        if len(messages) < SUMMARY_TRIGGER_ROUNDS:
            return None

        self._ensure_schema()

        # 幂等检查：同会话已存摘要且轮数不少于本次 → 跳过。
        # 前端可能多次整体保存同一会话，不幂等会反复插摘要导致记忆污染。
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT message_count FROM conversation_memories "
                "WHERE user_id = %s AND session_id = %s",
                (user_id, session_id),
            )
            row = cur.fetchone()
        if row is not None and row[0] is not None and row[0] >= len(messages):
            logger.debug(
                f"记忆已存在且对话未增长，跳过写入: session={session_id[:8]}... "
                f"(stored={row[0]}, now={len(messages)})"
            )
            return None

        # 拼对话文本
        conv_text = self._format_conversation(messages)

        # LLM 生成摘要（格式化对话内容到 system prompt 中）
        formatted_prompt = _SUMMARY_PROMPT.format(conversation=conv_text)
        summary = self._llm.chat("请按照系统提示的格式生成摘要。", system_prompt=formatted_prompt)

        # 解析实体 + 重要度预筛
        entities = self._parse_entities(summary)
        importance = self._estimate_importance(len(messages))

        # 向量化
        summary_vec = self._embedder.embed_query(summary)

        # UPSERT 写入 pgvector（幂等：同 (user_id, session_id) 覆盖并刷新 TTL）
        self._ensure_connection()
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_memories "
                "(user_id, session_id, summary, summary_embed, entities, message_count, importance) "
                "VALUES (%s, %s, %s, %s::halfvec, %s::jsonb, %s, %s) "
                "ON CONFLICT (user_id, session_id) "
                "DO UPDATE SET summary = EXCLUDED.summary, "
                "  summary_embed = EXCLUDED.summary_embed, "
                "  entities = EXCLUDED.entities, "
                "  message_count = EXCLUDED.message_count, "
                "  importance = EXCLUDED.importance, "
                "  expires_at = NOW() + INTERVAL '%s days'",
                (
                    user_id,
                    session_id,
                    summary,
                    summary_vec,
                    json.dumps(entities or {}, ensure_ascii=False),
                    len(messages),
                    importance,
                    MEMORY_TTL_DAYS,
                ),
            )
        self._conn.commit()
        logger.info(
            f"对话记忆已保存: user={user_id[:8]}..., session={session_id[:8]}..., "
            f"msg_count={len(messages)}, importance={importance}"
        )
        return summary

    # ------------------------------------------------------------------
    # 记忆检索
    # ------------------------------------------------------------------

    @_locked
    def retrieve(
        self,
        user_id: str,
        query: str,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[dict]:
        """检索与当前问题最相关的历史对话摘要

        1. embedding 查询
        2. pgvector 余弦相似度检索（仅查该用户的记忆 — 多租户硬隔离）
        3. 排序打分：score = relevance × importance_norm × decay(t)
           - relevance: 向量相似度
           - importance: 写入时按轮数分档的重要度（归一化到 [0.5, 1.0]）
           - decay: 指数时间衰减 e^{-λt}（半衰期 7 天）

        Returns:
            [{"summary", "entities", "score", "importance", "message_count", "created_at"}, ...]
        """
        query_vec = self._embedder.embed_query(query)

        self._ensure_connection()
        self._ensure_schema()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT summary, entities, message_count, created_at, importance, "
                "1 - (summary_embed <=> %s::halfvec) AS score "
                "FROM conversation_memories "
                "WHERE user_id = %s "
                "  AND expires_at > NOW() "
                "ORDER BY summary_embed <=> %s::halfvec "
                "LIMIT %s",
                (query_vec, user_id, query_vec, top_k),
            )
            rows = cur.fetchall()

        if not rows:
            return []

        import datetime
        import math

        now = datetime.datetime.now(datetime.timezone.utc)
        # 指数衰减：半衰期 7 天，λ = ln2 / 7
        decay_lambda = math.log(2) / max(TIME_DECAY_DAYS, 1)
        results = []
        for row in rows:
            summary, entities, msg_count, created_at, importance, score = row

            # 1. 时间衰减（指数形式，遗忘曲线更平滑）
            if created_at:
                age_days = max((now - created_at).days, 0)
                decay = math.exp(-decay_lambda * age_days)
                score = score * decay

            # 2. importance 加权（归一化到 [0.5, 1.0]，避免过低重要度完全压掉相关度）
            imp = float(importance) if importance is not None else self._estimate_importance(msg_count or 0)
            imp_norm = 0.5 + 0.5 * imp
            score = score * imp_norm

            results.append({
                "summary": summary,
                "entities": entities or {},
                "score": round(float(score), 4),
                "importance": round(float(imp), 4),
                "message_count": msg_count,
                "created_at": created_at.isoformat() if created_at else None,
            })

        # 按衰减后分数重排
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def build_context(self, memories: list[dict]) -> str:
        """将检索到的记忆组装为 Prompt 上下文片段"""
        if not memories:
            return ""

        lines = ["## 历史对话参考（用户之前咨询过的相关内容）"]
        for i, m in enumerate(memories, 1):
            lines.append(f"### 历史对话 {i}（相关度: {m['score']:.2f}）")
            lines.append(m["summary"])
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _format_conversation(messages: list[dict]) -> str:
        """将消息列表格式化为对话文本"""
        lines = []
        for msg in messages:
            role = "用户" if msg.get("role") == "user" else "AI助手"
            content = msg.get("content", "")
            # 截断过长消息
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"{role}: {content}")
        return "\n\n".join(lines)

    @staticmethod
    def _parse_entities(summary: str) -> dict:
        """从摘要中提取关键实体"""
        entities = {}
        for line in summary.split("\n"):
            line = line.strip()
            if line.startswith("案件类型:"):
                entities["case_type"] = line.replace("案件类型:", "").strip()
            elif line.startswith("涉及法律:"):
                entities["laws_involved"] = line.replace("涉及法律:", "").strip()
            elif line.startswith("关键事实:"):
                entities["key_facts"] = line.replace("关键事实:", "").strip()
        return entities
