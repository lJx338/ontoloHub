import { useEffect, useRef, useState } from 'react'
import { ChevronDown, LogOut, User as UserIcon } from 'lucide-react'

interface Me {
  id: string
  email?: string
  display_name?: string
  is_admin?: boolean
}

export function UserMenu() {
  const [open, setOpen] = useState(false)
  const [me, setMe] = useState<Me | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    fetch('/api/auth/me', { credentials: 'include' })
      .then(async (r) => {
        if (!alive) return
        if (r.ok) setMe((await r.json()) as Me)
      })
      .catch(() => {
        /* 静默失败：未登录或 dev mode */
      })
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-slate-700 hover:bg-slate-100"
      >
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-200 text-slate-600">
          <UserIcon size={14} />
        </span>
        <span className="hidden sm:inline">
          {me?.display_name ?? me?.email ?? '访客'}
        </span>
        <ChevronDown size={14} className="text-slate-400" />
      </button>
      {open ? (
        <div className="absolute right-0 z-20 mt-1 w-48 rounded-md border border-slate-200 bg-white py-1 text-sm shadow-lg">
          {me ? (
            <div className="border-b border-slate-100 px-3 py-2">
              <div className="font-medium text-slate-800">{me.display_name ?? me.email}</div>
              {me.is_admin ? (
                <div className="text-xs text-sky-700">管理员</div>
              ) : null}
            </div>
          ) : null}
          <a
            href="/api/auth/logout"
            className="flex items-center gap-2 px-3 py-2 text-slate-700 hover:bg-slate-50"
          >
            <LogOut size={14} />
            退出登录
          </a>
        </div>
      ) : null}
    </div>
  )
}
