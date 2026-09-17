# OntoloHub API（apps/api）

FastAPI 后端。Python ≥ 3.11。

## 启动

```bash
# 激活 monorepo 根目录的 venv
cd ../..
source venv/bin/activate    # Windows: venv\Scripts\Activate.ps1

# 起服务
cd apps/api
python -m uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

或直接：
```bash
npm run dev:api
```

## 入口

- App：`src/api/main.py`
- 路由：`src/api/*.py`
- 领域模型：`src/db/`
- 配置：`src/core/config.py`（从 monorepo 根 `.env` 读取）

## 数据库

- M0 默认 SQLite，文件：`../../data/ontolohub.db`
- 切换 PostgreSQL：编辑 monorepo 根 `.env` 的 `DATABASE_URL`

## Alembic

```bash
cd apps/api
alembic upgrade head        # 跑迁移
alembic revision --autogenerate -m "..."   # 生成新迁移
```