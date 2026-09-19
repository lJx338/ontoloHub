import { useEffect, useState } from 'react'
import { ChevronDown, RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import type { ProjectRecord } from '../lib/types'

interface Props {
  value: string | null
  onChange: (projectId: string | null) => void
  className?: string
}

/**
 * 项目选择器：从 ``GET /projects`` 拉列表并缓存到 localStorage（last selection）。
 *
 * 当前用户必须至少是某个项目的成员才能看到该项目的 Action / Function。
 * 如果用户不属于任何项目，显示「请联系管理员将您加入项目」。
 */
export function ProjectSelector({ value, onChange, className = '' }: Props) {
  const [projects, setProjects] = useState<ProjectRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const list = await api.get<ProjectRecord[]>('/projects')
      setProjects(list)
      // 若当前选择不在列表里，自动选第一个
      if (list.length > 0) {
        const stillValid = value && list.some((p) => p.id === value)
        if (!stillValid) {
          onChange(list[0].id)
          localStorage.setItem('ontolohub:lastProject', list[0].id)
        }
      } else if (value !== null) {
        onChange(null)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    const saved = localStorage.getItem('ontolohub:lastProject')
    if (saved && !value) onChange(saved)
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div className="relative">
        <select
          aria-label="选择项目"
          value={value ?? ''}
          onChange={(e) => {
            const v = e.target.value || null
            onChange(v)
            if (v) localStorage.setItem('ontolohub:lastProject', v)
          }}
          className="appearance-none rounded-md border border-slate-300 bg-white py-1.5 pl-3 pr-9 text-sm text-slate-900 focus:border-sky-500 focus:outline-none"
        >
          {projects.length === 0 && <option value="">（暂无项目）</option>}
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} {p.my_role ? `· ${p.my_role}` : ''}
            </option>
          ))}
        </select>
        <ChevronDown
          size={14}
          className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-slate-400"
        />
      </div>
      <button
        type="button"
        onClick={() => void reload()}
        disabled={loading}
        className="rounded-md border border-slate-300 bg-white p-1.5 text-slate-600 hover:bg-slate-50 disabled:opacity-50"
        title="刷新项目列表"
      >
        <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
      </button>
      {error && <span className="text-xs text-rose-600">加载失败：{error}</span>}
    </div>
  )
}
