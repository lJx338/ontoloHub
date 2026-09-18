# OntoloHub API Dockerfile
# 用法：docker build -f apps/api/Dockerfile -t ontolohub-api .
# 或 docker compose up --build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系统依赖（asyncpg / uvicorn[standard] / cryptography 需要的）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 从 monorepo 根复制依赖清单（不在 WORKDIR 内，COPY ../ 不可用）
# 所以改用 pip 直接从 pyproject.toml 安装
COPY pyproject.toml ./

# 安装依赖（直接从 pyproject.toml；这样和 `pip install -e .` 行为一致）
RUN pip install --no-cache-dir \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.30.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.5.0" \
        "sqlalchemy>=2.0.0" \
        "asyncpg>=0.29.0" \
        "alembic>=1.13.0" \
        "aiosqlite>=0.20.0" \
        "pyshacl>=0.26.0" \
        "python-dotenv>=1.0.0" \
        "pyyaml>=6.0.0" \
        "python-multipart>=0.0.9" \
        "httpx>=0.27.0" \
        "rich>=13.7.0" \
        "typer>=0.12.0" \
        "cryptography>=42.0.0" \
        "redis[hiredis]>=5.0.0" \
        "python-jose[cryptography]>=3.3.0" \
        "passlib[bcrypt]>=1.7.4" \
        "bcrypt>=4.0.0,<5.0" \
        "email-validator>=2.0.0"

# 再拷代码
COPY alembic.ini ./
COPY src ./src
COPY alembic ./alembic

# 暴露端口
EXPOSE 8000

# 启动脚本
COPY start_api.sh /usr/local/bin/start_api.sh
RUN chmod +x /usr/local/bin/start_api.sh

HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["/usr/local/bin/start_api.sh"]
