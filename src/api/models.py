"""
API 请求/响应模型。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """单次问答请求"""
    query: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    top_k: int = Field(default=5, ge=1, le=20, description="检索条文数")
    history: list[dict] = Field(default_factory=list, description="多轮对话历史 [{role, content}]")
    session_id: str = Field(default="", description="会话 ID，客户端传入，服务端按用户隔离校验")
    request_id: str = Field(default="", description="请求唯一 ID，用于客户端取消该生成请求")


class CancelRequest(BaseModel):
    """取消生成请求：前端点击停止后通知后端立即中断对应 LLM 流"""
    request_id: str = Field(..., min_length=1, max_length=64, description="要取消的请求 ID")


class RewriteRequest(BaseModel):
    """查询改写请求：把口语化问题规范化为法律检索查询"""
    query: str = Field(..., min_length=1, max_length=2000, description="用户原始提问")


class ChatResponse(BaseModel):
    """单次问答响应"""
    query: str
    answer: str
    sources: list[dict] = Field(default_factory=list)
    is_casual: bool = False

    @classmethod
    def from_rag_answer(cls, query: str, answer: str, sources: list, is_casual: bool = False) -> "ChatResponse":
        return cls(
            query=query,
            answer=answer,
            is_casual=is_casual,
            sources=[
                {
                    "law_name": s.law_name,
                    "chapter": s.chapter,
                    "article_range": s.article_range,
                    "citation": s.citation,
                    "score": float(s.score),  # pgvector 返回 float，统一转 Python float
                    "content": getattr(s, "content", ""),  # 条文原文，供前端查看
                }
                for s in sources
            ],
        )


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    version: str
    index_ready: bool
    doc_count: int
    llm_model: str


class RegisterRequest(BaseModel):
    """用户注册请求"""
    username: str = Field(..., min_length=2, max_length=64, description="用户名")
    password: str = Field(..., min_length=6, max_length=128, description="密码，至少 6 位")


class LoginRequest(BaseModel):
    """用户登录请求"""
    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, max_length=128, description="密码")


class AuthResponse(BaseModel):
    """认证响应（注册/登录共用）"""
    user_id: str
    token: str
    username: str


class ErrorResponse(BaseModel):
    """统一错误响应"""
    error: str
    detail: str = ""
    code: str = "INTERNAL_ERROR"


class CrawlRequest(BaseModel):
    """爬取请求（数据源: 国家法律法规数据库）"""
    source: str = Field(default="npc", description="数据源，目前仅支持 npc(国家法律法规数据库)")
    doc_type: str = Field(
        default="law",
        description="文档类型（flk 顶级分类规范值）: constitution/law/regulation/supervision/local_regulation/judicial_interpretation/all",
    )
    keyword: str = Field(default="", description="标题模糊搜索关键词（空=该类型全部）")
    limit: int = Field(default=50, ge=0, le=1000, description="最多爬取条数，0=不限")
    force: bool = Field(default=False, description="是否强制重爬已存在的文档")
    subdir: str = Field(default="", description="覆盖输出子目录名（默认按 doc_type 自动）")
    store: str = Field(
        default="pg",
        description="输出目标: pg(pgvector，推荐) / txt(LawData 原始文本存档) / both。可组合如 pg,txt",
    )
    rebuild: bool = Field(default=False, description="爬完后是否重建 pgvector 索引（HNSW 增量已生效，一般无需开启）")


class CrawlTaskResponse(BaseModel):
    """爬取任务提交响应"""
    task_id: str
    status: str
    message: str


class CrawlStatusResponse(BaseModel):
    """爬取任务状态 / 结果"""
    task_id: str
    status: str
    progress: dict
    errors: list[str] = []
    files: list[str] = []
    finished: bool
    rebuild: str | None = None
    result: dict | None = None
