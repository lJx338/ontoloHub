import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
//
// 开发期 API 由 `npm run dev:api`（或 `npm run dev` 同时启动两者）启在 8000。
// 浏览器看到的 `/api/...` 与业务路径都会通过代理转发到 8000。
//
// 注意：vite proxy 不会自动 rewrite — 业务路由本来就以 /projects、/ontologies 等
// 开头，没有 /api 前缀，所以这里保持原样转发即可。
const proxyToApi = {
  target: 'http://localhost:8000',
  changeOrigin: true,
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': {
        ...proxyToApi,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/health': proxyToApi,
      // 业务路由（main.py 中直接 include_router，无 /api 前缀）
      '/projects': proxyToApi,
      '/ontologies': proxyToApi,
      '/catalog': proxyToApi,
      '/mappings': proxyToApi,
      '/sources': proxyToApi,
      '/proposals': proxyToApi,
      '/candidates': proxyToApi,
      '/validation': proxyToApi,
      '/evidences': proxyToApi,
      '/profiling': proxyToApi,
      '/objects': proxyToApi,
      '/connectors': proxyToApi,
      '/releases': proxyToApi,
      '/change-requests': proxyToApi,
      '/deployments': proxyToApi,
      '/audit': proxyToApi,
      '/workflows': proxyToApi,
      '/workflow-executions': proxyToApi,
    },
  },
})
