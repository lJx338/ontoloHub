@echo off
REM OntoloHub Windows 启动脚本
REM 用法：
REM   run.bat            默认模式（本地虚拟环境 + 已存在的 Postgres）
REM   run.bat docker     用 docker compose 启 Postgres + API
REM   run.bat local      同默认（保留以兼容旧用法）

set MODE=%1
if "%MODE%"=="" set MODE=local

if /I "%MODE%"=="docker" goto docker_mode
if /I "%MODE%"=="local" goto local_mode

:local_mode
REM Check if virtual environment exists
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

REM Check if .env exists
if not exist .env (
    echo Copying .env.example to .env...
    copy .env.example .env
)

REM Run the application
echo Starting OntoloHub API in local mode...
echo API will be available at http://localhost:8000
echo API docs at http://localhost:8000/api/docs
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
goto end

:docker_mode
echo Starting OntoloHub with Docker Compose...
where docker >nul 2>&1
if errorlevel 1 (
    echo Docker not found. Please install Docker Desktop first.
    exit /b 1
)
docker compose up
goto end

:end
endlocal
