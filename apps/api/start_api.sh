#!/bin/sh
# OntoloHub API 启动脚本（HIA-64 B1）
# 1. 用 Alembic 把 DB 升级到 head
# 2. 启动 uvicorn

set -e

echo "[startup] Running alembic upgrade head..."
python -m alembic -c alembic.ini upgrade head

echo "[startup] Starting uvicorn..."
exec uvicorn src.api.main:app --host 0.0.0.0 --port 8000
