import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { PageHeader } from '../components/PageHeader'

interface Health {
  status: string
  version: string
}

export function DashboardPage() {
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
      description="OntoloHub · M0 Native MVP · 项目骨架就位"
    >
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-xs uppercase tracking-wide text-slate-500">
            API 健康
          </div>
          <div className="mt-2 text-2xl font-semibold">
            {health ? health.status : error ? '异常' : '加载中…'}
          </div>
          <div className="mt-1 text-xs text-slate-500">
            版本 {health?.version ?? '—'}
          </div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-xs uppercase tracking-wide text-slate-500">
            后端地址
          </div>
          <div className="mt-2 text-sm">http://localhost:8000</div>
          <div className="mt-1 text-xs text-slate-500">Swagger /api/docs</div>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-xs uppercase tracking-wide text-slate-500">
            前端地址
          </div>
          <div className="mt-2 text-sm">http://localhost:3000</div>
          <div className="mt-1 text-xs text-slate-500">Vite dev server</div>
        </div>
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-base font-semibold">下一步</h2>
        <ul className="mt-2 list-inside list-disc text-sm text-slate-700">
          <li>HIA-51 — Project CRUD API（一切的前置）</li>
          <li>HIA-49 → HIA-53 → HIA-52 — 数据 → 剖析 → 候选（流水线）</li>
          <li>HIA-55 / HIA-56 — 证据收件箱 + 本体 CRUD</li>
          <li>HIA-58 / HIA-57 / HIA-61 / HIA-59 — 校验 / CR / Release / Object</li>
        </ul>
        <p className="mt-3 text-xs text-slate-500">
          完整路线图见{' '}
          <a
            href="https://linear.app/hiatt/project/ontolohub-a82602909d16"
            target="_blank"
            rel="noreferrer"
          >
            Linear / OntoloHub
          </a>
          。
        </p>
      </section>
    </PageHeader>
  )
}