import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Project } from '../lib/types'
import { PageHeader } from '../components/PageHeader'

export function ProjectsPage() {
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)

  function refresh() {
    setLoading(true)
    api
      .listProjects()
      .then(setProjects)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(refresh, [])

  return (
    <PageHeader
      title="项目"
      description="业务项目的容器；一切本体 / 证据 / 候选都挂在项目下。"
      actions={
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-sky-700"
        >
          <Plus size={14} />
          新建项目
        </button>
      }
    >
      {error ? (
        <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          {error}
        </div>
      ) : null}

      {showForm ? (
        <CreateProjectForm
          onCreated={(p) => {
            setShowForm(false)
            refresh()
            navigate(`/projects/${p.id}/overview`)
          }}
          onCancel={() => setShowForm(false)}
        />
      ) : null}

      <section className="rounded-lg border border-slate-200 bg-white">
        {loading ? (
          <div className="p-6 text-sm text-slate-500">加载中…</div>
        ) : projects.length === 0 ? (
          <div className="p-6 text-sm text-slate-500">
            还没有项目，点击右上角「新建项目」开始。
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {projects.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  onClick={() => navigate(`/projects/${p.id}/overview`)}
                  className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-slate-50"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-800">{p.name}</div>
                    {p.description ? (
                      <div className="truncate text-xs text-slate-500">{p.description}</div>
                    ) : null}
                  </div>
                  <div className="flex items-center gap-3 text-xs text-slate-500">
                    <span className="rounded-full bg-slate-100 px-2 py-0.5">{p.status}</span>
                    <span>{new Date(p.created_at).toLocaleDateString()}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </PageHeader>
  )
}

function CreateProjectForm({
  onCreated,
  onCancel,
}: {
  onCreated: (p: Project) => void
  onCancel: () => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const p = await api.createProject({
        name: name.trim(),
        description: description.trim() || undefined,
      })
      onCreated(p)
    } catch (e: unknown) {
      const msg =
        e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mb-4 rounded-lg border border-slate-200 bg-white p-4"
    >
      <h3 className="text-sm font-semibold text-slate-800">新建项目</h3>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="block text-sm">
          <span className="text-slate-600">名称</span>
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
            placeholder="例如：Acme CRM"
          />
        </label>
        <label className="block text-sm">
          <span className="text-slate-600">描述</span>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
            placeholder="可选"
          />
        </label>
      </div>
      {error ? (
        <div className="mt-3 rounded-md border border-rose-200 bg-rose-50 p-2 text-xs text-rose-700">
          {error}
        </div>
      ) : null}
      <div className="mt-3 flex items-center gap-2">
        <button
          type="submit"
          disabled={submitting || !name.trim()}
          className="rounded-md bg-sky-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-sky-300"
        >
          {submitting ? '创建中…' : '创建'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
        >
          取消
        </button>
      </div>
    </form>
  )
}
