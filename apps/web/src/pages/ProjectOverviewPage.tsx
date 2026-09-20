import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../lib/api'
import type { Project } from '../lib/types'
import { PageHeader } from '../components/PageHeader'

export function ProjectOverviewPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const [project, setProject] = useState<Project | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!projectId) return
    let alive = true
    setLoading(true)
    api
      .getProject(projectId)
      .then((p) => {
        if (alive) setProject(p)
      })
      .catch((e: unknown) => {
        if (alive) setError(String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [projectId])

  return (
    <PageHeader
      title={project?.name ?? '加载中…'}
      description={project?.description ?? undefined}
      actions={
        project ? (
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-700">
            {project.status}
          </span>
        ) : undefined
      }
    >
      {error ? (
        <div className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          加载项目失败：{error}
        </div>
      ) : null}

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <StatCard label="本体版本" value="—" hint="待 HIA-56 (A7) 完善" />
        <StatCard label="证据数" value="—" hint="待 HIA-49/53 完善" />
        <StatCard label="待审 CR" value="—" hint="待 HIA-57 (A10) 完善" />
        <StatCard label="验证运行" value="—" hint="待 HIA-58 (A9) 完善" />
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-base font-semibold">项目元信息</h2>
        {loading && !project ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : project ? (
          <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <Field label="ID" value={project.id} mono />
            <Field label="状态" value={project.status} />
            <Field label="客户" value={project.customer_name ?? '—'} />
            <Field label="目标环境" value={project.target_environment ?? '—'} />
            <Field label="负责人" value={project.owner_name ?? '—'} />
            <Field label="业务负责人" value={project.business_owner ?? '—'} />
            <Field label="创建时间" value={new Date(project.created_at).toLocaleString()} />
            <Field label="更新时间" value={new Date(project.updated_at).toLocaleString()} />
          </dl>
        ) : null}
      </section>
    </PageHeader>
  )
}

function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-2 text-2xl font-semibold text-slate-800">{value}</div>
      {hint ? <div className="mt-1 text-xs text-slate-500">{hint}</div> : null}
    </div>
  )
}

function Field({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-24 shrink-0 text-slate-500">{label}</dt>
      <dd className={['flex-1 text-slate-800', mono ? 'font-mono text-xs' : ''].join(' ')}>
        {value}
      </dd>
    </div>
  )
}
