import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { PageHeader } from '../components/PageHeader'

interface Health {
  status: string
  version: string
}

export function DashboardPage() {
  const navigate = useNavigate()
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((e: unknown) => setError(String(e)))
  }, [])

  return (
    <PageHeader
      title="工作台"
      description="OntoloHub · M0 Native MVP · 进入项目查看工作台"
    >
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-xs uppercase tracking-wide text-slate-500">API 健康</div>
          <div className="mt-2 text-2xl font-semibold">
            {health ? health.status : error ? '异常' : '加载中…'}
          </div>
          <div className="mt-1 text-xs text-slate-500">版本 {health?.version ?? '—'}</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-xs uppercase tracking-wide text-slate-500">后端地址</div>
          <div className="mt-2 text-sm">http://localhost:8000</div>
          <div className="mt-1 text-xs text-slate-500">
            <a href="/api/docs" target="_blank" rel="noreferrer">
              Swagger /api/docs →
            </a>
          </div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-xs uppercase tracking-wide text-slate-500">前端地址</div>
          <div className="mt-2 text-sm">http://localhost:3000</div>
          <div className="mt-1 text-xs text-slate-500">Vite dev server</div>
        </div>
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-base font-semibold">进入项目</h2>
        <p className="mt-2 text-sm text-slate-700">
          在「项目」页选择一个项目进入工作台，或创建一个新项目。
        </p>
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={() => navigate('/projects')}
            className="rounded-md bg-sky-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-sky-700"
          >
            打开项目列表
          </button>
        </div>
      </section>
    </PageHeader>
  )
}
