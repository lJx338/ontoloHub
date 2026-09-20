import { NavLink, Outlet } from 'react-router-dom'
import {
  Activity,
  Files,
  GitPullRequestArrow,
  Network,
  ShieldCheck,
} from 'lucide-react'
import { PageHeader } from './PageHeader'
import { ProjectSelector } from './ProjectSelector'
import { UserMenu } from './UserMenu'

const tabs = [
  { to: 'overview', label: '概览', icon: Activity },
  { to: 'evidence', label: '证据', icon: Files },
  { to: 'ontology', label: '本体', icon: Network },
  { to: 'verify', label: '验证', icon: ShieldCheck },
  { to: 'changes', label: '变更/发布', icon: GitPullRequestArrow },
] as const

export function ProjectLayout() {
  return (
    <div className="flex h-full flex-col">
      {/* Top bar */}
      <header className="flex items-center justify-between gap-4 border-b border-slate-200 bg-white px-6 py-3">
        <div className="flex items-center gap-6">
          <a href="/" className="text-base font-semibold tracking-tight text-slate-800">
            OntoloHub
          </a>
          <ProjectSelector />
        </div>
        <UserMenu />
      </header>

      {/* Project workbench tabs */}
      <div className="border-b border-slate-200 bg-white">
        <div className="flex items-center gap-1 px-6">
          {tabs.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === 'overview'}
              className={({ isActive }) =>
                [
                  '-mb-px flex items-center gap-2 border-b-2 px-3 py-2 text-sm transition',
                  isActive
                    ? 'border-sky-500 text-sky-700'
                    : 'border-transparent text-slate-600 hover:text-slate-900',
                ].join(' ')
              }
            >
              <Icon size={14} />
              {label}
            </NavLink>
          ))}
        </div>
      </div>

      {/* Content */}
      <main className="flex-1 overflow-auto bg-slate-50">
        <Outlet />
      </main>
    </div>
  )
}

interface PlaceholderProps {
  title: string
  description: string
  hia: string
}

/** A13 工作台内尚未由独立 HIA 落地的占位面板。 */
export function WorkbenchPlaceholder({ title, description, hia }: PlaceholderProps) {
  return (
    <PageHeader title={title} description={description}>
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        占位 · 由 <span className="font-mono">{hia}</span> 落地。
      </div>
    </PageHeader>
  )
}
