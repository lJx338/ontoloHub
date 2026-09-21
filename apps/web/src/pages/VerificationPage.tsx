import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  AlertTriangle,
  Check,
  FlaskConical,
  Loader2,
  Play,
  Save,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type {
  SHACLViolation,
  ValidationRun,
  ValidationRunDetail,
  ValidationStatus,
} from '../lib/types'
import { PageHeader } from '../components/PageHeader'

const STATUS_BADGE: Record<ValidationStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-sky-100 text-sky-700',
  SUCCEEDED: 'bg-emerald-100 text-emerald-700',
  FAILED: 'bg-rose-100 text-rose-700',
  CANCELLED: 'bg-slate-200 text-slate-500',
}

const SEVERITY_BADGE: Record<string, string> = {
  violation: 'bg-rose-100 text-rose-700',
  warning: 'bg-amber-100 text-amber-700',
  info: 'bg-sky-100 text-sky-700',
}

export function VerificationPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const pid = projectId ?? ''

  const [runs, setRuns] = useState<ValidationRun[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ValidationRunDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [savingQuery, setSavingQuery] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  // new-run form state
  const [newType, setNewType] = useState('shacl')
  const [newOntologyVersionId, setNewOntologyVersionId] = useState('')

  const refresh = useCallback(async () => {
    if (!pid) return
    setLoading(true)
    setError(null)
    try {
      const list = await api.listProjectValidationRuns(pid, 50)
      setRuns(list)
      if (!selectedId && list.length > 0) setSelectedId(list[0].id)
    } catch (e: unknown) {
      onErr(e, setError)
    } finally {
      setLoading(false)
    }
  }, [pid, selectedId])

  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

  // fetch selected run detail
  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let alive = true
    setDetailLoading(true)
    api
      .getValidationRun(selectedId)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch((e: unknown) => {
        if (alive) onErr(e, setError)
      })
      .finally(() => {
        if (alive) setDetailLoading(false)
      })
    return () => {
      alive = false
    }
  }, [selectedId])

  const handleCreate = useCallback(async () => {
    if (!pid) return
    setCreating(true)
    setError(null)
    setInfo(null)
    try {
      const created = await api.createValidationRun(pid, {
        validation_type: newType,
        ontology_version_id: newOntologyVersionId || undefined,
      })
      setInfo(`已创建运行 #${created.id.slice(0, 8)}。`)
      await refresh()
      setSelectedId(created.id)
    } catch (e: unknown) {
      onErr(e, setError)
    } finally {
      setCreating(false)
    }
  }, [pid, newType, newOntologyVersionId, refresh])

  const handleExecute = useCallback(async () => {
    if (!selectedId) return
    setExecuting(true)
    setError(null)
    setInfo(null)
    try {
      await api.executeValidationRun(selectedId)
      setInfo('已触发执行，请稍候刷新。')
      // 不立即 fetch — 异步任务，给后端时间跑
      setTimeout(() => {
        void refresh()
      }, 800)
    } catch (e: unknown) {
      onErr(e, setError)
    } finally {
      setExecuting(false)
    }
  }, [selectedId, refresh])

  const handleSaveAsQuery = useCallback(async () => {
    if (!selectedId) return
    const name = window.prompt('保存为查询名称', detail?.name ?? 'My Query')
    if (!name) return
    setSavingQuery(true)
    setError(null)
    setInfo(null)
    try {
      await api.saveValidationRunAsQuery(selectedId, { name })
      setInfo('已保存为可复用查询。')
    } catch (e: unknown) {
      onErr(e, setError)
    } finally {
      setSavingQuery(false)
    }
  }, [selectedId, detail])

  const conformance = useMemo(() => {
    if (!detail) return null
    if (detail.total_checks === 0) return null
    return ((detail.passed_checks / detail.total_checks) * 100).toFixed(1)
  }, [detail])

  return (
    <PageHeader
      title="验证"
      description="用 SHACL 对本体 / 映射版本跑验证，查看违规清单并保存为可复用查询。"
    >
      {error ? (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span className="break-all">{error}</span>
        </div>
      ) : null}
      {info ? (
        <div className="mb-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">
          {info}
        </div>
      ) : null}

      {/* 新建运行 */}
      <div className="mb-4 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-800">新建验证运行</h2>
        <div className="mt-3 flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">类型</span>
            <select
              value={newType}
              onChange={(e) => setNewType(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            >
              <option value="shacl">SHACL</option>
              <option value="constraint">Constraint</option>
              <option value="mapping">Mapping</option>
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">
              Ontology Version ID（可选）
            </span>
            <input
              type="text"
              value={newOntologyVersionId}
              onChange={(e) => setNewOntologyVersionId(e.target.value)}
              placeholder="uuid（可选）"
              className="w-72 rounded-md border border-slate-300 px-2 py-1 font-mono text-xs"
            />
          </label>
          <button
            type="button"
            onClick={() => void handleCreate()}
            disabled={creating || !pid}
            className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
          >
            {creating ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <FlaskConical size={12} />
            )}
            创建
          </button>
        </div>
      </div>

      {loading ? (
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
          加载运行历史…
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_1fr]">
          {/* 历史列表 */}
          <aside className="rounded-lg border border-slate-200 bg-white">
            <header className="border-b border-slate-100 px-3 py-2 text-sm font-semibold text-slate-800">
              运行历史 ({runs.length})
            </header>
            {runs.length === 0 ? (
              <div className="p-4 text-sm text-slate-500">
                还没有运行。
              </div>
            ) : (
              <ul className="divide-y divide-slate-100">
                {runs.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(r.id)}
                      className={[
                        'block w-full px-3 py-2 text-left text-sm hover:bg-slate-50',
                        selectedId === r.id ? 'bg-sky-50' : '',
                      ].join(' ')}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs text-slate-700">
                          {r.validation_type || r.id.slice(0, 8)}
                        </span>
                        <span
                          className={[
                            'shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase',
                            STATUS_BADGE[r.status] ?? 'bg-slate-100 text-slate-700',
                          ].join(' ')}
                        >
                          {r.status.toLowerCase()}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] text-slate-500">
                        {r.total_tests} 测试 · {r.passed_tests} 通过 ·{' '}
                        {r.failed_tests} 失败 ·{' '}
                        {new Date(r.created_at).toLocaleString()}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </aside>

          {/* 详情 */}
          <section className="space-y-4">
            {!selectedId ? (
              <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
                左侧选择一次运行查看详情。
              </div>
            ) : detailLoading && !detail ? (
              <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
                加载运行详情…
              </div>
            ) : detail ? (
              <RunDetail
                detail={detail}
                conformance={conformance}
                executing={executing}
                savingQuery={savingQuery}
                onExecute={() => void handleExecute()}
                onSaveAsQuery={() => void handleSaveAsQuery()}
                onRefresh={() => void refresh()}
              />
            ) : null}
          </section>
        </div>
      )}
    </PageHeader>
  )
}

function RunDetail({
  detail,
  conformance,
  executing,
  savingQuery,
  onExecute,
  onSaveAsQuery,
  onRefresh,
}: {
  detail: ValidationRunDetail
  conformance: string | null
  executing: boolean
  savingQuery: boolean
  onExecute: () => void
  onSaveAsQuery: () => void
  onRefresh: () => void
}) {
  const violations: SHACLViolation[] = detail.violations ?? []
  const isDone =
    detail.status === 'SUCCEEDED' ||
    detail.status === 'FAILED' ||
    detail.status === 'CANCELLED'

  return (
    <>
      <div className="rounded-lg border border-slate-200 bg-white">
        <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold text-slate-800">
              {detail.name || `Run ${detail.id.slice(0, 8)}`}
            </h3>
            <p className="mt-0.5 text-xs text-slate-500">
              {detail.validation_type} ·{' '}
              {detail.duration_ms != null ? `${detail.duration_ms} ms` : '—'} ·{' '}
              {detail.started_at
                ? new Date(detail.started_at).toLocaleString()
                : '未开始'}
            </p>
          </div>
          <span
            className={[
              'shrink-0 rounded px-2 py-1 text-xs uppercase',
              STATUS_BADGE[detail.status] ?? 'bg-slate-100 text-slate-700',
            ].join(' ')}
          >
            {detail.status.toLowerCase()}
          </span>
        </header>

        <div className="px-4 py-3">
          {/* 进度条 */}
          <div className="flex items-center justify-between text-xs text-slate-600">
            <span>Conformance</span>
            <span className="font-mono">{conformance ?? '—'}%</span>
          </div>
          <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-slate-100">
            <div
              className={[
                'h-full transition-all',
                detail.failed_checks > 0
                  ? 'bg-rose-500'
                  : detail.warning_checks > 0
                    ? 'bg-amber-500'
                    : 'bg-emerald-500',
              ].join(' ')}
              style={{
                width: `${conformance ?? 0}%`,
              }}
            />
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
            <Stat label="Total" value={detail.total_checks} />
            <Stat label="Passed" value={detail.passed_checks} accent="emerald" />
            <Stat label="Warning" value={detail.warning_checks} accent="amber" />
            <Stat label="Failed" value={detail.failed_checks} accent="rose" />
          </div>

          {/* 操作 */}
          <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-100 pt-3">
            <button
              type="button"
              disabled={executing || isDone}
              onClick={onExecute}
              className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
            >
              {executing ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Play size={12} />
              )}
              {isDone ? '已结束' : '执行'}
            </button>
            <button
              type="button"
              disabled={savingQuery || detail.status !== 'SUCCEEDED'}
              onClick={onSaveAsQuery}
              className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              title={
                detail.status !== 'SUCCEEDED'
                  ? '仅成功的运行可保存为查询'
                  : undefined
              }
            >
              {savingQuery ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Save size={12} />
              )}
              保存为查询
            </button>
            <button
              type="button"
              onClick={onRefresh}
              className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            >
              刷新
            </button>
          </div>
        </div>
      </div>

      {/* Violations 表格 */}
      <div className="rounded-lg border border-slate-200 bg-white">
        <header className="flex items-center justify-between border-b border-slate-100 px-4 py-2">
          <h3 className="text-sm font-semibold text-slate-800">
            Violations ({violations.length})
          </h3>
        </header>
        {violations.length === 0 ? (
          <div className="p-6 text-sm text-slate-500">
            {detail.status === 'SUCCEEDED'
              ? '✓ 无违规。'
              : '运行尚未完成，或没有 violation 数据。'}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Severity</th>
                  <th className="px-3 py-2 font-medium">Focus Node</th>
                  <th className="px-3 py-2 font-medium">Path</th>
                  <th className="px-3 py-2 font-medium">Message</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {violations.map((v, i) => (
                  <tr key={i} className="hover:bg-slate-50">
                    <td className="px-3 py-2">
                      <span
                        className={[
                          'rounded px-1.5 py-0.5 text-[10px] uppercase',
                          SEVERITY_BADGE[String(v.severity ?? '').toLowerCase()] ??
                            'bg-slate-100 text-slate-700',
                        ].join(' ')}
                      >
                        {String(v.severity ?? '?').toLowerCase()}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-700">
                      {v.focus_node ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-700">
                      {v.path ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-xs text-slate-700">
                      {v.message ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string
  value: number
  accent?: 'emerald' | 'amber' | 'rose'
}) {
  const color =
    accent === 'emerald'
      ? 'text-emerald-700'
      : accent === 'amber'
        ? 'text-amber-700'
        : accent === 'rose'
          ? 'text-rose-700'
          : 'text-slate-700'
  return (
    <div className="rounded border border-slate-100 bg-slate-50 p-2">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div className={['mt-0.5 text-lg font-semibold', color].join(' ')}>
        {value}
      </div>
    </div>
  )
}

function onErr(
  e: unknown,
  setError: (msg: string | null) => void,
): void {
  if (e instanceof ApiError) {
    setError(`${e.status} ${JSON.stringify(e.body)}`)
  } else {
    setError(String(e))
  }
}

// keep Check import alive (used implicitly elsewhere; ensures tree-shake
// doesn't flag it for some bundler configurations)
const _Check = Check
void _Check
