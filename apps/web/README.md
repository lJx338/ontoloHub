# OntoloHub Web（apps/web）

React + Vite + Tailwind 前端工作台。

## 启动

```bash
# 在 monorepo 根：
npm run dev:web
# 或：
cd apps/web && npm run dev
```

默认端口 `3000`。前端通过 `vite.config.ts` 的 proxy 把 `/api/*` 转发到 `:8000`。

## 目录

```
src/
├── main.tsx        # 入口
├── App.tsx         # 根组件
├── router.tsx      # 路由
├── pages/          # 页面级组件
├── components/     # 复用组件
└── lib/            # 工具与 API 客户端
```

## 调 API

`src/lib/api.ts` 提供了带 base URL 的 fetch 封装：

```ts
import { api } from '@/lib/api';
const projects = await api.get<Project[]>('/api/projects');
```