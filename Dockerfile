FROM python:3.12-slim

WORKDIR /app

# 系统依赖 (psycopg2 编译需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 先复制依赖文件，利用 Docker 缓存
COPY pyproject.toml uv.lock ./

# 安装依赖
RUN uv sync --frozen --no-dev

# 复制源码
COPY src/ ./src/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY LawData/ ./LawData/

# 运行时
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
