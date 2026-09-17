#!/bin/bash
# OntoloHub Unix/macOS 启动脚本
#
# 用法：
#   ./run.sh            默认（本地虚拟环境 + 已存在的 Postgres）
#   ./run.sh docker     用 docker compose 启 Postgres + API
#   ./run.sh local      同默认

set -e

MODE=${1:-local}

case "$MODE" in
  docker)
    echo "Starting OntoloHub with Docker Compose..."
    command -v docker >/dev/null 2>&1 || {
      echo "Docker not found. Please install Docker Desktop first." >&2
      exit 1
    }
    docker compose up
    ;;
  local|*)
    # Check if virtual environment exists
    if [ ! -d "venv" ]; then
      echo "Creating virtual environment..."
      python3 -m venv venv
      # shellcheck disable=SC1091
      source venv/bin/activate
      pip install -r requirements.txt
    else
      # shellcheck disable=SC1091
      source venv/bin/activate
    fi

    # Check if .env exists
    if [ ! -f ".env" ]; then
      echo "Copying .env.example to .env..."
      cp .env.example .env
    fi

    echo "Starting OntoloHub API in local mode..."
    echo "API will be available at http://localhost:8000"
    echo "API docs at http://localhost:8000/api/docs"
    uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
    ;;
esac
