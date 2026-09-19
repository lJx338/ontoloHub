import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ChevronDown, FolderKanban } from 'lucide-react'
import { api } from '../lib/api'
import type { Project } from '../lib/types'

export function ProjectSelector() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    api
      .listProjects()
      .then((data) => {
        if (alive) setProjects(data)
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
  }, [])

  const current = projects.find((p) => p.id === projectId)

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 shadow-sm hover:bg-slate-50"
      >
        <FolderKanban size={16} className="text-slate-500" />
        <span className="max-w-[260px] truncate">
          {current ? current.name : loading ? '加载项目…' : '选择项目'}
        </span>
        <ChevronDown size={14} className="text-slate-400" />
      </button>
      {open ? (
        <div
          className="absolute left-0 z-20 mt-1 max-h-80 w-80 overflow-auto rounded-md border border-slate-200 bg-white shadow-lg"
          onMouseLeave={() => setOpen(false)}
        >
          {error ? (
            <div className="px-3 py-2 text-xs text-rose-600">无法加载项目：{error}</div>
          ) : projects.length === 0 ? (
            <div className="px-3 py-2 text-xs text-slate-500">
              还没有项目，<a href="/projects" className="text-sky-700 underline">去创建</a>
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {projects.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setOpen(false)
                      navigate(`/projects/${p.id}/overview`)
                    }}
                    className={[
                      'flex w-full flex-col items-start px-3 py-2 text-left text-sm hover:bg-slate-50',
                      p.id === projectId ? 'bg-sky-50' : '',
                    ].join(' ')}
                  >
                    <span className="font-medium text-slate-800">{p.name}</span>
                    <span className="text-xs text-slate-500">
                      {p.status} · {p.my_role ?? '—'}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  )
}
