import { useEffect, useMemo, useState } from 'react'
import { Loader2, Play, Plus, Save, Search, Trash2 } from 'lucide-react'
import { PageHeader } from '../components/PageHeader'
import { ProjectSelector } from '../components/ProjectSelector'
import { CodeEditor } from '../components/CodeEditor'
import { JsonView } from '../components/JsonView'
import { api } from '../lib/api'
import type {
  ActionKind,
  ActionRunRecord,
  ActionTypeRecord,
} from '../lib/types'

const KIND_LABELS: Record<ActionKind, string> = {
  function: '函数',
  webhook: 'Webhook',
  workflow: 'Workflow',
}

const DEFAULT_PYTHON_ACTION = `def handler(input_data, context):
    """Action 入口。返回 dict，会作为 output_data。"""
    return {"echo": input_data, "ok": True}
`

const DEFAULT_JS_ACTION = `async function handler(input_data, context) {
  return { echo: input_data, ok: true };
}
`

const DEFAULT_WEBHOOK_CONFIG = {
  url: 'https://httpbin.org/post',
  method: 'POST',
  timeout: 15,
}

export function ActionsPage() {
  const [projectId, setProjectId] = useState<string | null>(null)
  const [actions, setActions] = useState<ActionTypeRecord[]>([])
  const [search, setSearch] = useState('')
  const [kindFilter, setKindFilter] = useState<ActionKind | 'all'>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [draft, setDraft] = useState<ActionTypeRecord | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  // Run
  const [runInput, setRunInput] = useState<string>('{}\n')
  const [running, setRunning] = useState(false)
  const [lastRun, setLastRun] = useState<ActionRunRecord | null>(null)
  const [runs, setRuns] = useState<ActionRunRecord[]>([])
  const [polling, setPolling] = useState(false)

  const filtered = useMemo(
    () =>
      actions.filter((a) => {
        if (kindFilter !== 'all' && a.kind !== kindFilter) return false
        if (search && !`${a.name} ${a.description ?? ''}`.toLowerCase().includes(search.toLowerCase())) return false
        return true
      }),
    [actions, search, kindFilter],
  )

  // ---- 列表 ----
  const reloadActions = async (pid: string) => {
    setLoading(true)
    setError(null)
    try {
      const list = await api.get<ActionTypeRecord[]>(`/projects/${pid}/actions`)
      setActions(list)
      const stillValid = list.find((a) => a.id === selectedId)
      const next = stillValid ?? list[0] ?? null
      setSelectedId(next?.id ?? null)
      setDraft(next ? { ...next } : null)
      setDirty(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (projectId) void reloadActions(projectId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  // ---- 创建 ----
  const createAction = async () => {
    if (!projectId) return
    const name = window.prompt('Action 名称')?.trim()
    if (!name) return
    const kindRaw = window.prompt('Kind（function / webhook / workflow）', 'function')?.trim().toLowerCase()
    const kind: ActionKind | null =
      kindRaw === 'function' || kindRaw === 'webhook' || kindRaw === 'workflow' ? kindRaw : null
    if (!kind) {
      window.alert('Kind 必须是 function / webhook / workflow 之一')
      return
    }

    const body: Record<string, unknown> = { name, kind }
    if (kind === 'function') {
      body.runtime = window.confirm('使用 JavaScript？点「取消」选 Python') ? 'javascript' : 'python'
      body.code = body.runtime === 'javascript' ? DEFAULT_JS_ACTION : DEFAULT_PYTHON_ACTION
    } else if (kind === 'webhook') {
      body.config = { ...DEFAULT_WEBHOOK_CONFIG }
    }
    try {
      const created = await api.post<ActionTypeRecord>(`/projects/${projectId}/actions`, body)
      await reloadActions(projectId)
      setSelectedId(created.id)
      setDraft(created)
      setDirty(false)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      window.alert(`创建失败：${msg}`)
    }
  }

  // ---- 删除 ----
  const deleteAction = async (a: ActionTypeRecord) => {
    if (!projectId) return
    if (!window.confirm(`确认删除 Action '${a.name}'？`)) return
    try {
      await api.delete(`/projects/${projectId}/actions/${a.id}`)
      setSelectedId(null)
      setDraft(null)
      await reloadActions(projectId)
    } catch (e) {
      window.alert(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // ---- 保存 ----
  const saveDraft = async () => {
    if (!projectId || !draft) return
    setSaving(true)
    try {
      const updated = await api.patch<ActionTypeRecord>(
        `/projects/${projectId}/actions/${draft.id}`,
        {
          name: draft.name,
          description: draft.description,
          status: draft.status,
          parameters_schema: draft.parameters_schema,
          return_schema: draft.return_schema,
          code: draft.code,
          runtime: draft.runtime,
          config: draft.config,
        },
      )
      setDraft({ ...updated })
      setDirty(false)
      await reloadActions(projectId)
    } catch (e) {
      window.alert(`保存失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  // ---- 运行 ----
  const runAction = async () => {
    if (!projectId || !draft) return
    let parsed: unknown
    try {
      parsed = runInput.trim() ? JSON.parse(runInput) : {}
    } catch (e) {
      window.alert(`输入不是合法 JSON：${e instanceof Error ? e.message : String(e)}`)
      return
    }
    setRunning(true)
    setLastRun(null)
    try {
      const created = await api.post<ActionRunRecord>(
        `/projects/${projectId}/actions/${draft.id}/run`,
        { input_data: parsed },
      )
      setLastRun(created)
      // 启动轮询（async backend）
      startPolling(created.id)
    } catch (e) {
      setLastRun({
        id: '00000000-0000-0000-0000-000000000000',
        project_id: projectId,
        action_type_id: draft.id,
        status: 'failed',
        input_data: parsed as Record<string, unknown>,
        output_data: null,
        error: e instanceof Error ? e.message : String(e),
        started_at: null,
        completed_at: null,
        duration_ms: null,
        triggered_by: null,
        created_at: new Date().toISOString(),
      })
    } finally {
      setRunning(false)
    }
  }

  const startPolling = (runId: string) => {
    setPolling(true)
    let aborted = false
    const tick = async () => {
      for (let i = 0; i < 30 && !aborted; i++) {
        try {
          const run = await api.get<ActionRunRecord>(`/action-runs/${runId}`)
          setLastRun(run)
          if (run.status !== 'pending' && run.status !== 'running') {
            // 拉取历史
            const list = await api.get<ActionRunRecord[]>(`/projects/${projectId}/action-runs`, {
              action_type_id: draft?.id,
            })
            setRuns(list.slice(0, 20))
            setPolling(false)
            return
          }
        } catch {
          // 单次失败继续重试
        }
        await new Promise((r) => setTimeout(r, 1000))
      }
      setPolling(false)
    }
    void tick()
    return () => {
      aborted = true
    }
  }

  useEffect(() => {
    if (!projectId || !selectedId) {
      setRuns([])
      return
    }
    api
      .get<ActionRunRecord[]>(`/projects/${projectId}/action-runs`, { action_type_id: selectedId })
      .then((list) => setRuns(list.slice(0, 20)))
      .catch(() => setRuns([]))
  }, [projectId, selectedId])

  // ---- 渲染 ----
  return (
    <PageHeader
      title="Actions"
      description="项目内的可执行操作：函数代码 / Webhook / Workflow（C4）；支持试运行与历史。"
      actions={<ProjectSelector value={projectId} onChange={setProjectId} />}
    >
      {!projectId ? (
        <EmptyHint>请先选择一个项目以查看其 Action。</EmptyHint>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
          {/* 左：列表 */}
          <aside className="flex flex-col rounded-lg border border-slate-200 bg-white">
            <div className="space-y-2 border-b border-slate-200 p-2">
              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" />
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="搜索 name / description"
                    className="w-full rounded-md border border-slate-300 bg-white py-1.5 pl-7 pr-2 text-sm focus:border-sky-500 focus:outline-none"
                  />
                </div>
                <button
                  onClick={createAction}
                  className="flex items-center gap-1 rounded-md bg-sky-600 px-2 py-1.5 text-xs font-medium text-white hover:bg-sky-700"
                  title="新建 Action"
                >
                  <Plus size={14} /> 新建
                </button>
              </div>
              <div className="flex gap-1 text-xs">
                {(['all', 'function', 'webhook', 'workflow'] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setKindFilter(k)}
                    className={[
                      'rounded px-2 py-0.5',
                      kindFilter === k ? 'bg-sky-100 text-sky-700' : 'bg-slate-100 text-slate-600 hover:bg-slate-200',
                    ].join(' ')}
                  >
                    {k === 'all' ? '全部' : KIND_LABELS[k as ActionKind]}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex-1 overflow-auto">
              {loading ? (
                <div className="flex items-center justify-center p-6 text-xs text-slate-500">
                  <Loader2 size={14} className="mr-1 animate-spin" /> 加载中…
                </div>
              ) : error ? (
                <div className="p-3 text-xs text-rose-600">错误：{error}</div>
              ) : filtered.length === 0 ? (
                <div className="p-6 text-center text-xs text-slate-500">暂无 Action。</div>
              ) : (
                <ul className="divide-y divide-slate-100">
                  {filtered.map((a) => (
                    <li
                      key={a.id}
                      className={[
                        'flex cursor-pointer items-start justify-between gap-2 px-3 py-2 text-sm hover:bg-slate-50',
                        selectedId === a.id ? 'bg-sky-50' : '',
                      ].join(' ')}
                      onClick={() => {
                        setSelectedId(a.id)
                        setDraft({ ...a })
                        setDirty(false)
                        setLastRun(null)
                      }}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate font-medium text-slate-900">{a.name}</span>
                          <StatusPill status={a.status} />
                        </div>
                        <div className="truncate text-xs text-slate-500">
                          {KIND_LABELS[a.kind]} · v{a.version}
                          {a.kind === 'function' && a.runtime ? ` · ${a.runtime}` : ''}
                        </div>
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          void deleteAction(a)
                        }}
                        className="rounded p-1 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                        title="删除"
                      >
                        <Trash2 size={14} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </aside>

          {/* 右：详情 */}
          <section className="flex flex-col gap-4">
            {!draft ? (
              <EmptyHint>左侧选择一个 Action 来编辑，或点击「新建」。</EmptyHint>
            ) : (
              <>
                <div className="rounded-lg border border-slate-200 bg-white p-4">
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <Field label="名称">
                      <input
                        value={draft.name}
                        onChange={(e) => {
                          setDraft({ ...draft, name: e.target.value })
                          setDirty(true)
                        }}
                        className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none"
                      />
                    </Field>
                    <Field label="Kind / Status">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="rounded bg-slate-100 px-2 py-1 font-mono text-xs">{draft.kind}</span>
                        <select
                          value={draft.status}
                          onChange={(e) => {
                            setDraft({ ...draft, status: e.target.value as ActionTypeRecord['status'] })
                            setDirty(true)
                          }}
                          className="rounded border border-slate-300 bg-white px-2 py-1 text-xs"
                        >
                          <option value="draft">draft</option>
                          <option value="published">published</option>
                          <option value="deprecated">deprecated</option>
                        </select>
                        <span className="ml-auto text-xs text-slate-500">
                          {dirty ? '● 未保存' : '已同步'}
                        </span>
                      </div>
                    </Field>
                    <Field label="描述" full>
                      <input
                        value={draft.description ?? ''}
                        onChange={(e) => {
                          setDraft({ ...draft, description: e.target.value || null })
                          setDirty(true)
                        }}
                        className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none"
                      />
                    </Field>
                  </div>
                </div>

                {/* 按 kind 渲染编辑区 */}
                {draft.kind === 'function' && (
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <div className="mb-2 flex items-center justify-between">
                      <h3 className="text-sm font-medium text-slate-700">代码（runtime: {draft.runtime}）</h3>
                      <button
                        onClick={saveDraft}
                        disabled={!dirty || draft.status !== 'draft' || saving}
                        className="flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                        title={draft.status !== 'draft' ? '非 draft 状态下代码不可改' : ''}
                      >
                        {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                        保存
                      </button>
                    </div>
                    <CodeEditor
                      value={draft.code ?? ''}
                      onChange={(next) => {
                        setDraft({ ...draft, code: next })
                        setDirty(true)
                      }}
                      language={(draft.runtime as 'python' | 'javascript') ?? 'python'}
                      minRows={14}
                      readOnly={draft.status !== 'draft'}
                      aria-label="Action 代码"
                    />
                    {draft.status !== 'draft' && (
                      <p className="mt-2 text-xs text-amber-600">
                        非 draft 状态下代码为只读；先切回 draft 才能修改。
                      </p>
                    )}
                  </div>
                )}

                {draft.kind === 'webhook' && (
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <h3 className="mb-2 text-sm font-medium text-slate-700">Webhook 配置</h3>
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                      <Field label="URL">
                        <input
                          value={(draft.config?.url as string) ?? ''}
                          onChange={(e) => {
                            setDraft({
                              ...draft,
                              config: { ...(draft.config ?? {}), url: e.target.value },
                            })
                            setDirty(true)
                          }}
                          className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 font-mono text-xs focus:border-sky-500 focus:outline-none"
                        />
                      </Field>
                      <Field label="Method">
                        <select
                          value={((draft.config?.method as string) ?? 'POST').toUpperCase()}
                          onChange={(e) => {
                            setDraft({
                              ...draft,
                              config: { ...(draft.config ?? {}), method: e.target.value },
                            })
                            setDirty(true)
                          }}
                          className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none"
                        >
                          {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="Timeout (s)">
                        <input
                          type="number"
                          min={1}
                          max={120}
                          value={Number(draft.config?.timeout ?? 15)}
                          onChange={(e) => {
                            setDraft({
                              ...draft,
                              config: { ...(draft.config ?? {}), timeout: Number(e.target.value) },
                            })
                            setDirty(true)
                          }}
                          className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none"
                        />
                      </Field>
                    </div>
                    <div className="mt-3">
                      <button
                        onClick={saveDraft}
                        disabled={!dirty || saving}
                        className="flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                      >
                        {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                        保存
                      </button>
                    </div>
                  </div>
                )}

                {draft.kind === 'workflow' && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
                    Workflow 编排由 HIA-76 (C4) 实现。本页面目前只能查看元数据，无法编辑步骤。
                  </div>
                )}

                {/* 运行 + 历史 */}
                {draft.kind !== 'workflow' && (
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <div className="mb-2 flex items-center justify-between">
                      <h3 className="text-sm font-medium text-slate-700">运行（异步）</h3>
                      <button
                        onClick={runAction}
                        disabled={running || polling}
                        className="flex items-center gap-1 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
                      >
                        {running || polling ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                        Run
                      </button>
                    </div>
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                      <div>
                        <div className="mb-1 text-xs font-medium text-slate-600">input_data (JSON)</div>
                        <CodeEditor
                          value={runInput}
                          onChange={setRunInput}
                          language="json"
                          minRows={8}
                          aria-label="运行输入 JSON"
                        />
                      </div>
                      <div>
                        <div className="mb-1 text-xs font-medium text-slate-600">最近一次</div>
                        {lastRun ? (
                          <div className="space-y-2">
                            <ResultLine label="状态" value={lastRun.status} tone={statusTone(lastRun.status)} />
                            <ResultLine label="耗时" value={lastRun.duration_ms ? `${lastRun.duration_ms} ms` : '—'} />
                            {lastRun.error && <ResultLine label="错误" value={lastRun.error} tone="rose" />}
                            {lastRun.output_data && (
                              <div>
                                <div className="text-xs font-medium text-slate-600">output_data</div>
                                <JsonView value={lastRun.output_data} defaultExpanded />
                              </div>
                            )}
                          </div>
                        ) : (
                          <div className="rounded-md border border-dashed border-slate-300 p-6 text-center text-xs text-slate-500">
                            点击「Run」触发异步执行
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                )}

                {/* 历史 */}
                {runs.length > 0 && (
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <h3 className="mb-2 text-sm font-medium text-slate-700">最近 20 次运行</h3>
                    <table className="w-full text-xs">
                      <thead className="text-left text-slate-500">
                        <tr>
                          <th className="py-1">时间</th>
                          <th className="py-1">状态</th>
                          <th className="py-1">耗时</th>
                          <th className="py-1">错误</th>
                        </tr>
                      </thead>
                      <tbody>
                        {runs.map((r) => (
                          <tr key={r.id} className="border-t border-slate-100">
                            <td className="py-1 font-mono text-slate-600">{formatTime(r.created_at)}</td>
                            <td className="py-1">
                              <StatusPill status={r.status} />
                            </td>
                            <td className="py-1 font-mono text-slate-600">{r.duration_ms ?? '—'} ms</td>
                            <td className="py-1 truncate text-rose-700" title={r.error ?? ''}>
                              {r.error ? r.error.slice(0, 80) : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </section>
        </div>
      )}
    </PageHeader>
  )
}

// ============ 小工具组件 ============

function Field({ label, children, full = false }: { label: string; children: React.ReactNode; full?: boolean }) {
  return (
    <label className={`block ${full ? 'md:col-span-2' : ''}`}>
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      {children}
    </label>
  )
}

function ResultLine({ label, value, tone = 'slate' }: { label: string; value: string; tone?: 'slate' | 'rose' | 'amber' | 'emerald' | 'sky' }) {
  const toneClass: Record<string, string> = {
    slate: 'text-slate-700',
    rose: 'text-rose-700',
    amber: 'text-amber-700',
    emerald: 'text-emerald-700',
    sky: 'text-sky-700',
  }
  return (
    <div className="flex items-baseline gap-2 text-xs">
      <span className="text-slate-500">{label}:</span>
      <span className={`font-mono ${toneClass[tone] ?? toneClass.slate}`}>{value}</span>
    </div>
  )
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">
      {children}
    </div>
  )
}

function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    draft: 'bg-slate-100 text-slate-700',
    published: 'bg-emerald-100 text-emerald-700',
    deprecated: 'bg-amber-100 text-amber-700',
    pending: 'bg-slate-100 text-slate-600',
    running: 'bg-sky-100 text-sky-700',
    success: 'bg-emerald-100 text-emerald-700',
    failed: 'bg-rose-100 text-rose-700',
    canceled: 'bg-slate-100 text-slate-500',
  }
  return (
    <span className={`rounded px-1.5 py-0.5 font-mono text-[10px] uppercase ${map[status] ?? 'bg-slate-100 text-slate-600'}`}>
      {status}
    </span>
  )
}

function statusTone(s: string): 'slate' | 'rose' | 'amber' | 'emerald' | 'sky' {
  if (s === 'success') return 'emerald'
  if (s === 'failed') return 'rose'
  if (s === 'running') return 'sky'
  if (s === 'pending') return 'slate'
  if (s === 'canceled') return 'slate'
  return 'slate'
}

function formatTime(iso: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString()
  } catch {
    return iso
  }
}
