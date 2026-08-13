-- ============================================================
-- Law-RAG-Agent v0.5 数据库初始化
-- PostgreSQL 15+ / pgvector 0.7+
-- ============================================================

-- 启用 pgvector 扩展（向量检索核心）
-- 支持 vector（全精度）、halfvec（半精度，存储减半）、sparsevec 三种向量类型
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. 用户表
-- ============================================================

-- 存储注册用户信息，支持 PBKDF2 密码哈希和 JWT token 哈希的双因子认证
CREATE TABLE IF NOT EXISTS users (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 用户唯一标识
    username        VARCHAR(64) UNIQUE NOT NULL,                  -- 登录用户名（唯一）
    password_hash   VARCHAR(256) NOT NULL DEFAULT '',             -- PBKDF2 密码哈希值（hex 编码）
    token_hash      VARCHAR(128) NOT NULL DEFAULT '',             -- JWT token 哈希值（服务端校验用）
    display_name    VARCHAR(128),                                 -- 显示名称（可选，用于前端展示）
    created_at      TIMESTAMPTZ DEFAULT now()                     -- 注册时间
);

-- 内置匿名用户，未登录时默认使用此 ID，避免外键约束报错
INSERT INTO users (id, username, password_hash, token_hash, display_name)
VALUES ('00000000-0000-0000-0000-000000000000', '__anonymous__', '', '', '匿名用户')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- 2. 对话表
-- ============================================================

-- 每个会话一条记录，全部消息以 JSONB 数组存储，支持按 (用户, 会话) 快速定位
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 记录唯一标识
    user_id         UUID NOT NULL REFERENCES users(id)           -- 所属用户（级联删除）
                        ON DELETE CASCADE
                        DEFAULT '00000000-0000-0000-0000-000000000000',
    session_id      TEXT NOT NULL,                                -- 前端生成的会话 UUID
    messages        JSONB NOT NULL DEFAULT '[]',                 -- 全部消息 [{role, content, timestamp}, ...]
    created_at      TIMESTAMPTZ DEFAULT now(),                    -- 会话创建时间
    updated_at      TIMESTAMPTZ DEFAULT now()                     -- 最后更新时间
);

-- 按 (用户, 会话) 唯一索引，保证每个用户的会话 ID 不重复
CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_user_session ON conversations(user_id, session_id);

-- ---------- 表/列注释（数据库可视化工具可见）----------
COMMENT ON TABLE users IS '用户认证表：存储注册用户信息，支持 PBKDF2 密码哈希 + JWT token 双向认证';
COMMENT ON COLUMN users.id IS '用户唯一标识 UUID';
COMMENT ON COLUMN users.username IS '登录用户名（唯一）';
COMMENT ON COLUMN users.password_hash IS 'PBKDF2 密码哈希值（hex 编码）';
COMMENT ON COLUMN users.token_hash IS 'JWT token 哈希值（服务端校验防篡改）';
COMMENT ON COLUMN users.display_name IS '显示名称（前端展示用）';
COMMENT ON COLUMN users.created_at IS '注册时间';

COMMENT ON TABLE conversations IS '对话记录表：每个会话一条记录，全部消息以 JSONB 存储';
COMMENT ON COLUMN conversations.id IS '记录唯一标识 UUID';
COMMENT ON COLUMN conversations.user_id IS '所属用户 ID（级联删除）';
COMMENT ON COLUMN conversations.session_id IS '前端生成的会话 UUID';
COMMENT ON COLUMN conversations.messages IS '全部消息 JSONB 数组 [{role, content, timestamp}]';
COMMENT ON COLUMN conversations.created_at IS '会话创建时间';
COMMENT ON COLUMN conversations.updated_at IS '最后更新时间';

-- ============================================================
-- 3. 知识库表（v0.5 企业级升级）
-- ============================================================

-- 3.1 文档主表
-- 存储法律文档的元信息，一个「法律/司法解释/案例/地方法规」对应一条记录
-- 支持版本管理和法律修订追踪（status 字段标记新旧版本）
CREATE TABLE IF NOT EXISTS documents (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 文档唯一标识
    doc_type        VARCHAR(40) NOT NULL,                        -- 文档类型(flk顶级分类): law(法律) | regulation(行政法规)
                                                                 --  judicial_interpretation(司法解释) | local_regulation(地方性法规)
                                                                 --  constitution(宪法) | supervision(监察法规) | case(案例)
    title           VARCHAR(500) NOT NULL,                       -- 文档标题（如"中华人民共和国刑法"）
    source          VARCHAR(500),                                -- 来源（如"全国人大"、"最高法"）
    effective_date  DATE,                                        -- 生效日期
    version         INT DEFAULT 1,                               -- 版本号（法律修订后递增）
    status          VARCHAR(20) DEFAULT 'active',                -- 状态: active(生效中) | superseded(已被替代)
                                                                 --       draft(草稿，未上线)
    superseded_by   UUID REFERENCES documents(id),               -- 替代此版本的文档 ID（法律修订时关联新版本）
    original_filename VARCHAR(500),                              -- 上传时的原始文件名（用于追溯）
    created_at      TIMESTAMPTZ DEFAULT now(),                    -- 创建时间
    updated_at      TIMESTAMPTZ DEFAULT now()                     -- 最后更新时间
);

-- 按文档类型+状态查询，支持"只看生效中的法律"等过滤
CREATE INDEX IF NOT EXISTS idx_docs_type_status ON documents(doc_type, status);

COMMENT ON TABLE documents IS '文档主表：法律/司法解释/案例/地方法规的元信息，支持版本管理和修订追踪';
COMMENT ON COLUMN documents.id IS '文档唯一标识 UUID';
COMMENT ON COLUMN documents.doc_type IS '文档类型(flk顶级分类): law | regulation | judicial_interpretation | local_regulation | constitution | supervision | case';
COMMENT ON COLUMN documents.title IS '文档标题（如"中华人民共和国刑法"）';
COMMENT ON COLUMN documents.source IS '来源（如"全国人大"、"最高法"）';
COMMENT ON COLUMN documents.effective_date IS '生效日期';
COMMENT ON COLUMN documents.version IS '版本号（法律修订后递增）';
COMMENT ON COLUMN documents.status IS '状态: active(生效中) | superseded(已被替代) | draft(草稿)';
COMMENT ON COLUMN documents.superseded_by IS '替代此版本的文档 ID（法律修订时关联新版本）';
COMMENT ON COLUMN documents.original_filename IS '上传时的原始文件名（用于追溯）';
COMMENT ON COLUMN documents.created_at IS '创建时间';
COMMENT ON COLUMN documents.updated_at IS '最后更新时间';

-- 3.2 文档块表（向量索引核心表）
-- 将文档按「条」切分后存入此表，每条都有独立的向量，支持语义检索
-- 使用 halfvec（半精度浮点）将向量存储减半，检索速度提升约 30-40%
-- embedding_model 列实现模型隔离，切换嵌入模型时无需全量重建旧数据
CREATE TABLE IF NOT EXISTS document_chunks (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 块唯一标识
    doc_id          UUID REFERENCES documents(id)                -- 所属文档（级联删除：删文档则块全删）
                        ON DELETE CASCADE,
    chunk_type      VARCHAR(40) NOT NULL,                        -- 块类型: article(法条) | 非条文体按 doc_type 存
                                                                 --  (case/judicial_interpretation/...) | judgment(判决要点)
                                                                 --         summary(章级摘要，检索时过滤) | guideline(指导要点)
    content         TEXT NOT NULL,                               -- 块的文本内容（以「条」为单位）
    embedding_model VARCHAR(50) NOT NULL,                        -- 向量化模型标识（如 "bge-m3"、"ollama:bge-m3"）
                                                                 -- 查询时 WHERE embedding_model = current 实现模型隔离
    embedding       HALFVEC(1024),                               -- 文本向量（半精度，bge-m3=1024维）
                                                                 -- 1024 维模型写入时 PG 自动补零适配
    metadata        JSONB,                                       -- 结构化元数据:
                                                                 --   {law_name, chapter, section, article_range, chunk_type}
    created_at      TIMESTAMPTZ DEFAULT now()                     -- 写入时间
);

-- HNSW 索引：检索速度优先，10万+ 向量仍保持 <10ms
-- m=16: 每节点最大 16 个邻居（平衡内存与速度）
-- ef_construction=200: 建索引时搜索范围（值越大索引质量越高，建得越慢）
-- halfvec_cosine_ops: 用半精度向量计算余弦相似度
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON document_chunks USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- 按文档 ID 快速查找该文档的所有块（用于文档删除/状态切换）
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(doc_id);

-- 按嵌入模型过滤（模型隔离 + 模型切换）
CREATE INDEX IF NOT EXISTS idx_chunks_model ON document_chunks(embedding_model);

COMMENT ON TABLE document_chunks IS '文档块表（向量索引核心）：按「条」切分的文档块，每条独立向量，halfvec 半精度存储减半';
COMMENT ON COLUMN document_chunks.id IS '块唯一标识 UUID';
COMMENT ON COLUMN document_chunks.doc_id IS '所属文档 ID（级联删除）';
COMMENT ON COLUMN document_chunks.chunk_type IS '块类型: article(法条) | 非条文体按 doc_type(case/judicial_interpretation等) | judgment(判决要点) | summary(章级摘要) | guideline(指导要点)';
COMMENT ON COLUMN document_chunks.content IS '块的文本内容（以「条」为单位）';
COMMENT ON COLUMN document_chunks.embedding_model IS '向量化模型标识（如"bge-m3"），查询时 WHERE embedding_model=current 实现模型隔离';
COMMENT ON COLUMN document_chunks.embedding IS '文本向量（半精度 halfvec，3072维为最大模型预留，小模型自动补零适配）';
COMMENT ON COLUMN document_chunks.metadata IS '结构化元数据 JSONB: {law_name, chapter, section, article_range, chunk_type}';
COMMENT ON COLUMN document_chunks.created_at IS '写入时间';

-- ============================================================
-- 4. FAQ 语义缓存表
-- ============================================================

-- 缓存高频问答，语义相似度 > 0.95 时直接返回缓存答案
-- 节省 LLM 调用成本，降低响应延迟（缓存命中时 <100ms）
-- TTL: 1 小时自动过期（命中自动续期）；关联法律修订时级联失效
CREATE TABLE IF NOT EXISTS faq_cache (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 缓存条目 ID
    question        TEXT NOT NULL,                               -- 用户原始问题
    question_embed  HALFVEC(1024),                               -- 问题向量（用于语义相似度匹配）
    answer          TEXT NOT NULL,                               -- 缓存的完整答案
    sources         JSONB,                                       -- 引用来源 [{law_name, article_range, score}, ...]
    related_laws    TEXT[],                                      -- 关联法律 ID 列表（修法时级联失效）
    confidence      FLOAT,                                       -- 回答置信度（低的缓存不写入）
    hit_count       INT DEFAULT 1,                               -- 命中次数（用于淘汰低频缓存）
    status          VARCHAR(20) DEFAULT 'active',                -- active(有效) | expired(TTL过期) | invalidated(修法失效)
    created_at      TIMESTAMPTZ DEFAULT now(),                    -- 创建时间
    expires_at      TIMESTAMPTZ                                  -- 过期时间（写入时设为 now() + 1 hour，命中自动续期）
);

-- HNSW 索引：快速找到语义相似的已缓存问题
CREATE INDEX IF NOT EXISTS idx_faq_embedding
    ON faq_cache USING hnsw (question_embed halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 200);

COMMENT ON TABLE faq_cache IS 'FAQ 语义缓存表：高频问答缓存，相似度>0.95 直接返回，TTL 1小时（命中自动续期），修法时级联失效';
COMMENT ON COLUMN faq_cache.id IS '缓存条目 ID';
COMMENT ON COLUMN faq_cache.question IS '用户原始问题';
COMMENT ON COLUMN faq_cache.question_embed IS '问题向量（用于语义相似度匹配）';
COMMENT ON COLUMN faq_cache.answer IS '缓存的完整答案';
COMMENT ON COLUMN faq_cache.sources IS '引用来源 JSON: [{law_name, article_range, score}]';
COMMENT ON COLUMN faq_cache.related_laws IS '关联法律 ID 数组（修法时级联失效）';
COMMENT ON COLUMN faq_cache.confidence IS '回答置信度（低置信度不缓存）';
COMMENT ON COLUMN faq_cache.hit_count IS '命中次数（淘汰低频缓存用）';
COMMENT ON COLUMN faq_cache.status IS '状态: active(有效) | expired(TTL过期) | invalidated(修法失效)';
COMMENT ON COLUMN faq_cache.created_at IS '创建时间';
COMMENT ON COLUMN faq_cache.expires_at IS '过期时间（写入时设为 now() + 1 hour，命中缓存自动续期刷新）';

-- ============================================================
-- 5. 对话记忆表
-- ============================================================

-- 存储跨会话的对话摘要，实现"一个月前的对话还能想起来"
-- 摘要由 LLM 在对话超过 6 轮时自动生成
-- TTL: 30 天自动过期
CREATE TABLE IF NOT EXISTS conversation_memories (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 记忆 ID
    user_id         VARCHAR(128) NOT NULL,                       -- 所属用户
    session_id      VARCHAR(128) NOT NULL,                       -- 原始会话 ID
    summary         TEXT,                                        -- LLM 生成的对话摘要（结构化）
    summary_embed   HALFVEC(1024),                               -- 摘要向量（用于语义检索历史对话）
    entities        JSONB,                                       -- 关键实体: {case_type, laws_involved, key_facts, ...}
    message_count   INT DEFAULT 0,                               -- 原始对话轮数
    importance      REAL DEFAULT 0.6,                            -- 重要度（按轮数分档预筛，检索时加权）
    created_at      TIMESTAMPTZ DEFAULT now(),                    -- 创建时间
    expires_at      TIMESTAMPTZ DEFAULT (now() + INTERVAL '30 days')  -- 30 天后自动清除
);

-- 按用户快速查找所有历史记忆
CREATE INDEX IF NOT EXISTS idx_memory_user ON conversation_memories(user_id);

-- 幂等唯一约束：同会话只保留一份记忆（save_memory 用 UPSERT 巩固更新）
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_user_session ON conversation_memories(user_id, session_id);

-- HNSW 索引：新问题时语义检索最相关的历史对话摘要
CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON conversation_memories USING hnsw (summary_embed halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 200);

COMMENT ON TABLE conversation_memories IS '对话记忆表：存储跨会话的对话摘要，LLM 在 >6 轮时自动生成，TTL 30天自动过期';
COMMENT ON COLUMN conversation_memories.id IS '记忆 ID';
COMMENT ON COLUMN conversation_memories.user_id IS '所属用户';
COMMENT ON COLUMN conversation_memories.session_id IS '原始会话 ID';
COMMENT ON COLUMN conversation_memories.summary IS 'LLM 生成的对话摘要（结构化文本）';
COMMENT ON COLUMN conversation_memories.summary_embed IS '摘要向量（用于语义检索历史对话）';
COMMENT ON COLUMN conversation_memories.entities IS '关键实体 JSONB: {case_type, laws_involved, key_facts}';
COMMENT ON COLUMN conversation_memories.message_count IS '原始对话轮数';
COMMENT ON COLUMN conversation_memories.importance IS '重要度（按轮数分档: 6/10/15 轮对应 0.6/0.8/1.0），检索时参与加权';
COMMENT ON COLUMN conversation_memories.created_at IS '创建时间';
COMMENT ON COLUMN conversation_memories.expires_at IS '过期时间（30天后自动清除）';

-- ============================================================
-- 6. 检索质量日志表（可观测性）
-- ============================================================

-- 每次查询记录完整的性能指标和检索链路信息
-- 用于：性能瓶颈分析 / 检索质量追踪 / 高频问题发现 / 成本核算
CREATE TABLE IF NOT EXISTS query_logs (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,  -- 日志 ID
    request_id      UUID NOT NULL,                               -- 请求唯一标识（与 API 日志关联）
    user_id         VARCHAR(128),                                -- 发起查询的用户
    query           TEXT NOT NULL,                               -- 用户原始查询
    intent          VARCHAR(20),                                 -- 意图分类: law_lookup | case_query | casual | other
    retrieved_count INT,                                         -- 粗排召回数（检索后、精排前）
    reranked_count  INT,                                         -- 精排后返回数
    faq_cache_hit   BOOLEAN DEFAULT FALSE,                       -- 是否命中 FAQ 缓存
    memory_docs_used INT DEFAULT 0,                              -- 本次查询使用的记忆文档数
    llm_tokens_used INT,                                         -- LLM 消耗的 token 总数（输入+输出）
    total_latency_ms INT,                                        -- 总耗时（毫秒）
    stage_timings   JSONB,                                       -- 各阶段耗时:
                                                                 --   {intent_ms, memory_ms, rewrite_ms, retrieve_ms,
                                                                 --    rerank_ms, generate_ms}
    created_at      TIMESTAMPTZ DEFAULT now()                     -- 记录时间
);

COMMENT ON TABLE query_logs IS '检索质量日志表：每次查询记录完整性能指标和检索链路，用于性能分析、质量追踪、成本核算';
COMMENT ON COLUMN query_logs.id IS '日志 ID';
COMMENT ON COLUMN query_logs.request_id IS '请求唯一标识（与 API 日志关联）';
COMMENT ON COLUMN query_logs.user_id IS '发起查询的用户';
COMMENT ON COLUMN query_logs.query IS '用户原始查询文本';
COMMENT ON COLUMN query_logs.intent IS '意图分类: law_lookup | case_query | casual | other';
COMMENT ON COLUMN query_logs.retrieved_count IS '粗排召回数（检索后、精排前）';
COMMENT ON COLUMN query_logs.reranked_count IS '精排后最终返回数';
COMMENT ON COLUMN query_logs.faq_cache_hit IS '是否命中 FAQ 缓存';
COMMENT ON COLUMN query_logs.memory_docs_used IS '本次查询使用的记忆文档数';
COMMENT ON COLUMN query_logs.llm_tokens_used IS 'LLM 消耗的 token 总数（输入+输出）';
COMMENT ON COLUMN query_logs.total_latency_ms IS '总耗时（毫秒）';
COMMENT ON COLUMN query_logs.stage_timings IS '各阶段耗时 JSONB: {intent_ms, memory_ms, rewrite_ms, retrieve_ms, rerank_ms, generate_ms}';
COMMENT ON COLUMN query_logs.created_at IS '记录时间';
