import { NavLink, Outlet } from 'react-router-dom'
import {
  LayoutDashboard,
  FolderKanban,
  Network,
  Inbox,
  ShieldCheck,
  Rocket,
} from 'lucide-react'

const nav = [
  { to: '/', label: '工作台', icon: LayoutDashboard, end: true },
  { to: '/projects', label: '项目', icon: FolderKanban },
  { to: '/ontologies', label: '本体', icon: Network },
  { to: '/evidence', label: '证据收件箱', icon: Inbox },
  { to: '/validation', label: '验证', icon: ShieldCheck },
  { to: '/releases', label: '变更/发布', icon: Rocket },
]

export function Layout() {
  return (
    <div className="flex h-full">
      <aside className="flex w-56 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-4 py-4">
          <div className="text-lg font-semibold">OntoloHub</div>
          <div className="text-xs text-slate-500">M0 · Native MVP</div>
        </div>
        <nav className="flex-1 space-y-1 px-2 py-3">
          {nav.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                [
                  'flex items-center gap-2 rounded px-3 py-2 text-sm transition',
                  isActive
                    ? 'bg-sky-100 text-sky-900'
                    : 'text-slate-700 hover:bg-slate-100',
                ].join(' ')
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-200 px-4 py-3 text-xs text-slate-500">
          <a href="/api/docs" target="_blank" rel="noreferrer">
            API Swagger →
          </a>
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}