/**
 * 轻量 fetch 封装，避开 axios / react-query 之类的额外依赖。
 *
 * Vite proxy 在开发期把所有业务路径（/projects, /ontologies 等）和 /api/* 转发到
 * :8000；生产期由反向代理（Nginx/Caddy）做同样的事情。所以前端代码永远用相对路径。
 *
 * 注意：默认 ``credentials: 'include'``，因此 ``Set-Cookie``（如 session / JWT cookie）
 * 能正常往返。
 */

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown
  readonly url: string

  constructor(status: number, detail: unknown, url: string) {
    const message =
      typeof detail === 'string'
        ? detail
        : detail && typeof detail === 'object' && 'detail' in (detail as Record<string, unknown>)
        ? String((detail as Record<string, unknown>).detail)
        : JSON.stringify(detail)
    super(`API ${status}: ${message}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.url = url
  }
}

type Primitive = string | number | boolean
type QueryValue = Primitive | undefined | null

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, QueryValue>
  headers?: Record<string, string>
  signal?: AbortSignal
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  if (!qs) return path
  return path + (path.includes('?') ? '&' : '?') + qs
}

export async function apiFetch<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, headers, signal } = opts
  const url = buildUrl(path, query)

  const res = await fetch(url, {
    method,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(headers ?? {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  })

  if (!res.ok) {
    let detail: unknown
    try {
      detail = await res.json()
    } catch {
      try {
        detail = await res.text()
      } catch {
        detail = res.statusText
      }
    }
    throw new ApiError(res.status, detail, url)
  }

  if (res.status === 204) return undefined as T
  // 部分端点可能返回空 body（不应有，但兜底）
  const text = await res.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

// 便捷方法
export const api = {
  get: <T = unknown>(path: string, query?: Record<string, QueryValue>) =>
    apiFetch<T>(path, { method: 'GET', query }),
  post: <T = unknown>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'POST', body }),
  patch: <T = unknown>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PATCH', body }),
  put: <T = unknown>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PUT', body }),
  delete: <T = unknown>(path: string) => apiFetch<T>(path, { method: 'DELETE' }),
  health: () => apiFetch<{ status: string; version: string; redis?: { enabled: boolean; healthy: boolean } }>('/health'),
}
