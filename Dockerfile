FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系统依赖（asyncpg / uvicorn[standard] 需要的）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，单独一层以利用缓存
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 再拷代码
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY scripts ./scripts

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动：先升级 DB，再启 uvicorn
CMD ["sh", "-c", "python scripts/init_db.py && uvicorn src.api.main:app --host 0.0.0.0 --port 8000"]
