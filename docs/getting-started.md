# 开发者上手

> 第一次跑通端到端的清单 — 大约 10 分钟。

## 0. 前置

确认版本：

```bash
python --version   # 3.11+
node --version     # 20+
npm --version
git --version
```

## 1. 克隆与安装

```bash
git clone <repo-url> ontoloHub
cd ontoloHub

# Windows
.\install.ps1 -Dev

# macOS / Linux
./install.sh --dev
```

完成后会看到：

```
✓ Python 3.12.x
✓ Node.js 22.x
✓ venv 已创建
✓ runtime 依赖已安装
✓ dev 依赖已安装
✓ apps/api (editable)
✓ apps/cli (editable)
✓ packages/core (editable)
✓ node_modules 已就位
✓ Alembic 已执行到 head
```

## 2. 启动 dev 服务

```bash
npm run dev
```

你会看到两个进程：

```
[api]  INFO:     Uvicorn running on http://0.0.0.0:8000
[web]  VITE v5.x  ready in 800 ms
[web]   ➜  Local:   http://localhost:3000/
```

浏览器访问：

| 地址 | 用途 |
|---|---|
| <http://localhost:3000> | 前端工作台 |
| <http://localhost:8000/api/docs> | API Swagger UI |
| <http://localhost:8000/health> | API 健康 |

## 3. 跑一次端到端（手动验证）

打开终端，分别执行：

```bash
# 健康检查
curl http://localhost:8000/health

# 创建一个项目（占位接口 — HIA-51 实现）
curl -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"name":"示例工厂","description":"端到端验证项目"}'
```

> HIA-50 阶段仅要求 API 起得来、/health 工作；
> 真正 CRUD 在 HIA-51 (Project CRUD) 之后。

## 4. CLI

```bash
ontolohub version
ontolohub doctor
ontolohub db status
ontolohub db migrate --to head
```

## 5. 测试套件

```bash
pytest                          # 后端
npm run lint                    # 前端
```

## 6. 清理（重建前的常规动作）

```bash
npm run clean                   # 删 node_modules / __pycache__ / 构建产物
```

---

## 常见问题

**Q：`alembic upgrade head` 报"relation does not exist"？**
A：这是首次迁移。确认 `apps/api/alembic/versions/` 里只有 `2026_09_16_0001_baseline.py` 且没有别的脏文件。重新跑 `ontolohub db migrate`。

**Q：`uvicorn` 启动后立刻退？**
A：多半是端口占用。改 `.env` 里的 `API_PORT=8001`，前端 vite.config.ts 同步改 proxy 端口。

**Q：Web 端 `/api/*` 返回 404？**
A：检查 `apps/web/vite.config.ts` 的 proxy 配置 —— 默认代理到 `:8001`，但 API 默认 `:8000`。

**Q：怎么切 PostgreSQL？**
A：M1 之前的过渡方案：装个本地 PG（`docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16`），`.env` 里改 `DATABASE_URL`，跑 `ontolohub db migrate`。

## 下一步

读完 [`docs/architecture.md`](architecture.md) 了解分层，然后从 Linear 项目 [OntoloHub](https://linear.app/hiatt/project/ontolohub-a82602909d16) 选一张卡开搞。

> 写新端点 / 改 schema 前，先看一遍 [`docs/DEVELOPMENT.md`](DEVELOPMENT.md) 的 §2 / §3 / §6，里面列的坑都在这个仓库里真实发生过。