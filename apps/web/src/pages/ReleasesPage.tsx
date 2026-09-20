import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  AlertTriangle,
  Check,
  GitBranch,
  GitMerge,
  Loader2,
  PackageCheck,
  Rocket,
  ScrollText,
  X,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type {
  AuditEvent,
  ChangeRequest,
  ChangeRequestStatus,
  Deployment,
  DeploymentStatus,
  Release,
  ReleaseStatus,
} from '../lib/types'
import { PageHeader } from '../components/PageHeader'

type Tab = 'cr' | 'releases' | 'deployments' | 'audit'

const CR_STATUS_BADGE: Record<ChangeRequestStatus, string> = {
  DRAFT: 'bg-slate-100 text-slate-700',
  SUBMITTED: 'bg-sky-100 text-sky-700',
  CHANGES_REQUESTED: 'bg-amber-100 text-amber-700',
  APPROVED: 'bg-emerald-100 text-emerald-700',
  MERGED: 'bg-violet-100 text-violet-700',
  CLOSED: 'bg-slate-200 text-slate-500',
}

const RELEASE_STATUS_BADGE: Record<ReleaseStatus, string> = {
  DRAFT: 'bg-slate-100 text-slate-700',
  PUBLISHED: 'bg-emerald-100 text-emerald-700',
  SUPERSEDED: 'bg-slate-200 text-slate-500',
  YANKED: 'bg-rose-100 text-rose-700',
  DEPRECATED: 'bg-amber-100 text-amber-700',
}

const DEPLOY_STATUS_BADGE: Record<DeploymentStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-sky-100 text-sky-700',
  SUCCEEDED: 'bg-emerald-100 text-emerald-700',
  FAILED: 'bg-rose-100 text-rose-700',
  ROLLED_BACK: 'bg-amber-100 text-amber-700',
}

const AUDIT_TYPE_BADGE: Record<string, string> = {
  CREATE: 'bg-emerald-100 text-emerald-700',
  UPDATE: 'bg-sky-100 text-sky-700',
  DELETE: 'bg-rose-100 text-rose-700',
  READ: 'bg-slate-100 text-slate-600',
  LOGIN: 'bg-violet-100 text-violet-700',
  LOGOUT: 'bg-slate-200 text-slate-500',
}

export function ReleasesPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const pid = projectId ?? ''
  const [tab, setTab] = useState<Tab>('cr')
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  return (
    <PageHeader
      title="变更 / 发布 / 部署"
      description="Change Request 工作流 → 版本发布 → 环境部署，全程可审计。"
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

      <div className="mb-4 border-b border-slate-200">
        <nav className="flex gap-1" aria-label="tabs">
          <TabButton
            active={tab === 'cr'}
            onClick={() => setTab('cr')}
            icon={<GitBranch size={14} />}
            label="Change Requests"
          />
          <TabButton
            active={tab === 'releases'}
            onClick={() => setTab('releases')}
            icon={<PackageCheck size={14} />}
            label="Releases"
          />
          <TabButton
            active={tab === 'deployments'}
            onClick={() => setTab('deployments')}
            icon={<Rocket size={14} />}
            label="Deployments"
          />
          <TabButton
            active={tab === 'audit'}
            onClick={() => setTab('audit')}
            icon={<ScrollText size={14} />}
            label="Audit"
          />
        </nav>
      </div>

      {tab === 'cr' ? (
        <ChangeRequestsTab
          projectId={pid}
          onError={setError}
          onInfo={setInfo}
        />
      ) : tab === 'releases' ? (
        <ReleasesTab projectId={pid} onError={setError} />
      ) : tab === 'deployments' ? (
        <DeploymentsTab projectId={pid} onError={setError} />
      ) : (
        <AuditTab projectId={pid} onError={setError} />
      )}
    </PageHeader>
  )
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        '-mb-px flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm transition',
        active
          ? 'border-sky-500 font-medium text-sky-700'
          : 'border-transparent text-slate-500 hover:text-slate-700',
      ].join(' ')}
    >
      {icon}
      {label}
    </button>
  )
}

// ===== Change Requests Tab =====

function ChangeRequestsTab({
  projectId,
  onError,
  onInfo,
}: {
  projectId: string
  onError: (msg: string | null) => void
  onInfo: (msg: string | null) => void
}) {
  const [items, setItems] = useState<ChangeRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!projectId) return
    setLoading(true)
    onError(null)
    try {
      const list = await api.listProjectChangeRequests(projectId)
      setItems(list)
      if (!selectedId && list.length > 0) setSelectedId(list[0].id)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
      onError(msg)
    } finally {
      setLoading(false)
    }
  }, [projectId, selectedId, onError])

  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) ?? null,
    [items, selectedId],
  )

  const decide = useCallback(
    async (action: 'submit' | 'approve' | 'reject' | 'merge') => {
      if (!selected) return
      setBusyId(selected.id)
      onError(null)
      onInfo(null)
      try {
        if (action === 'submit') await api.submitChangeRequest(selected.id)
        else if (action === 'approve')
          await api.approveChangeRequest(selected.id)
        else if (action === 'reject')
          await api.rejectChangeRequest(selected.id)
        else await api.mergeChangeRequest(selected.id)
        onInfo(
          action === 'submit'
            ? '已提交评审。'
            : action === 'approve'
              ? '已批准。'
              : action === 'reject'
                ? '已驳回。'
                : '已合并。',
        )
        await refresh()
      } catch (e: unknown) {
        const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
        onError(msg)
      } finally {
        setBusyId(null)
      }
    },
    [selected, onError, onInfo, refresh],
  )

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载中…
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        当前项目还没有 Change Request。CR 通常由本体编辑或证据决策触发。
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[360px_1fr]">
      <aside className="rounded-lg border border-slate-200 bg-white">
        <ul className="divide-y divide-slate-100">
          {items.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => setSelectedId(c.id)}
                className={[
                  'block w-full px-3 py-2 text-left text-sm hover:bg-slate-50',
                  selectedId === c.id ? 'bg-sky-50' : '',
                ].join(' ')}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-medium text-slate-800">
                    {c.title}
                  </span>
                  <span
                    className={[
                      'shrink-0 rounded px-1.5 py-0.5 text-[10px] uppercase',
                      CR_STATUS_BADGE[c.status] ?? 'bg-slate-100 text-slate-700',
                    ].join(' ')}
                  >
                    {c.status.toLowerCase()}
                  </span>
                </div>
                <div className="mt-0.5 truncate text-[11px] text-slate-500">
                  {c.baseline_version ?? '—'} → {c.target_version ?? '—'} ·{' '}
                  {new Date(c.created_at).toLocaleString()}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      {selected ? (
        <section className="space-y-4">
          <div className="rounded-lg border border-slate-200 bg-white p-4">
            <header className="flex items-start justify-between gap-4">
              <div>
                <h3 className="text-base font-semibold text-slate-800">
                  {selected.title}
                </h3>
                <p className="mt-0.5 text-xs text-slate-500">
                  {selected.description ?? '（无描述）'}
                </p>
              </div>
              <span
                className={[
                  'shrink-0 rounded px-2 py-1 text-xs uppercase',
                  CR_STATUS_BADGE[selected.status] ??
                    'bg-slate-100 text-slate-700',
                ].join(' ')}
              >
                {selected.status.toLowerCase()}
              </span>
            </header>
            <dl className="mt-4 grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <Field
                label="Baseline"
                value={selected.baseline_version ?? '—'}
                mono
              />
              <Field
                label="Target"
                value={selected.target_version ?? '—'}
                mono
              />
              <Field
                label="Required Approvers"
                value={String(selected.required_approvers)}
              />
              <Field
                label="Submitted"
                value={
                  selected.submitted_at
                    ? new Date(selected.submitted_at).toLocaleString()
                    : '—'
                }
              />
              <Field
                label="Approved"
                value={
                  selected.approved_at
                    ? new Date(selected.approved_at).toLocaleString()
                    : '—'
                }
              />
              <Field
                label="Merged"
                value={
                  selected.merged_at
                    ? new Date(selected.merged_at).toLocaleString()
                    : '—'
                }
              />
            </dl>
            {selected.changes_summary ? (
              <p className="mt-3 rounded bg-slate-50 p-2 text-xs text-slate-600">
                {selected.changes_summary}
              </p>
            ) : null}
            <div className="mt-4 flex flex-wrap gap-2">
              {selected.status === 'DRAFT' ? (
                <button
                  type="button"
                  disabled={busyId === selected.id}
                  onClick={() => void decide('submit')}
                  className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
                >
                  {busyId === selected.id ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <GitBranch size={12} />
                  )}
                  提交评审
                </button>
              ) : null}
              {selected.status === 'SUBMITTED' ||
              selected.status === 'CHANGES_REQUESTED' ? (
                <>
                  <button
                    type="button"
                    disabled={busyId === selected.id}
                    onClick={() => void decide('approve')}
                    className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                  >
                    {busyId === selected.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <Check size={12} />
                    )}
                    批准
                  </button>
                  <button
                    type="button"
                    disabled={busyId === selected.id}
                    onClick={() => void decide('reject')}
                    className="flex items-center gap-1.5 rounded-md border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                  >
                    {busyId === selected.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <X size={12} />
                    )}
                    驳回
                  </button>
                </>
              ) : null}
              {selected.status === 'APPROVED' ? (
                <button
                  type="button"
                  disabled={busyId === selected.id}
                  onClick={() => void decide('merge')}
                  className="flex items-center gap-1.5 rounded-md bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-700 disabled:opacity-50"
                >
                  {busyId === selected.id ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <GitMerge size={12} />
                  )}
                  合并
                </button>
              ) : null}
            </div>
          </div>

          <CrDiffSection crId={selected.id} />
        </section>
      ) : null}
    </div>
  )
}

function CrDiffSection({ crId }: { crId: string }) {
  const [diff, setDiff] = useState<Awaited<
    ReturnType<typeof api.getChangeRequestDiff>
  > | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    api
      .getChangeRequestDiff(crId)
      .then((d) => {
        if (alive) setDiff(d)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [crId])

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-500">
        加载 diff…
      </div>
    )
  }
  if (error) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-700">
        diff 加载失败：{error}
      </div>
    )
  }
  if (!diff) return null

  const rows: Array<{
    kind: 'added' | 'modified' | 'removed'
    type: 'class' | 'property'
    payload: Record<string, unknown>
  }> = []
  diff.added_classes?.forEach((c) =>
    rows.push({ kind: 'added', type: 'class', payload: c }),
  )
  diff.modified_classes?.forEach((c) =>
    rows.push({ kind: 'modified', type: 'class', payload: c }),
  )
  diff.removed_classes?.forEach((c) =>
    rows.push({ kind: 'removed', type: 'class', payload: c }),
  )
  diff.added_properties?.forEach((p) =>
    rows.push({ kind: 'added', type: 'property', payload: p }),
  )
  diff.modified_properties?.forEach((p) =>
    rows.push({ kind: 'modified', type: 'property', payload: p }),
  )
  diff.removed_properties?.forEach((p) =>
    rows.push({ kind: 'removed', type: 'property', payload: p }),
  )

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <header className="border-b border-slate-100 px-4 py-2 text-sm font-semibold text-slate-800">
        Diff（{diff.baseline_version ?? '—'} → {diff.target_version ?? '—'}）
      </header>
      {rows.length === 0 ? (
        <div className="p-4 text-sm text-slate-500">
          无内容变更。
        </div>
      ) : (
        <ul className="divide-y divide-slate-100">
          {rows.map((r, i) => {
            const iri = (r.payload.iri as string) ?? `item-${i}`
            const name = (r.payload.name as string) ?? iri
            const color =
              r.kind === 'added'
                ? 'text-emerald-700'
                : r.kind === 'removed'
                  ? 'text-rose-700'
                  : 'text-amber-700'
            const sign =
              r.kind === 'added' ? '+' : r.kind === 'removed' ? '-' : '~'
            return (
              <li
                key={`${r.type}-${iri}-${i}`}
                className="flex items-start gap-3 px-4 py-2 text-sm"
              >
                <span className={['shrink-0 font-mono text-xs', color].join(' ')}>
                  {sign} {r.type}
                </span>
                <span className="font-mono text-xs text-slate-700">
                  {name}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

// ===== Releases Tab =====

function ReleasesTab({
  projectId,
  onError,
}: {
  projectId: string
  onError: (msg: string | null) => void
}) {
  const [items, setItems] = useState<Release[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!projectId) return
    let alive = true
    setLoading(true)
    api
      .listProjectReleases(projectId)
      .then((r) => {
        if (alive) setItems(r)
      })
      .catch((e: unknown) => {
        if (alive) {
          const msg =
            e instanceof ApiError
              ? `${e.status} ${JSON.stringify(e.body)}`
              : String(e)
          onError(msg)
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [projectId, onError])

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载中…
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        当前项目还没有发布。
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2 font-medium">Version</th>
            <th className="px-3 py-2 font-medium">Ontology</th>
            <th className="px-3 py-2 font-medium">Mapping</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Released</th>
            <th className="px-3 py-2 font-medium">Size</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {items.map((r) => (
            <tr key={r.id} className="hover:bg-slate-50">
              <td className="px-3 py-2 font-mono text-xs">{r.version}</td>
              <td className="px-3 py-2 font-mono text-xs">
                {r.ontology_version ?? '—'}
              </td>
              <td className="px-3 py-2 font-mono text-xs">
                {r.mapping_version ?? '—'}
              </td>
              <td className="px-3 py-2">
                <span
                  className={[
                    'rounded px-1.5 py-0.5 text-[10px] uppercase',
                    RELEASE_STATUS_BADGE[r.status] ??
                      'bg-slate-100 text-slate-700',
                  ].join(' ')}
                >
                  {r.status.toLowerCase()}
                </span>
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {r.released_at
                  ? new Date(r.released_at).toLocaleString()
                  : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {r.artifact_size != null
                  ? `${(r.artifact_size / 1024).toFixed(1)} KB`
                  : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ===== Deployments Tab =====

function DeploymentsTab({
  projectId,
  onError,
}: {
  projectId: string
  onError: (msg: string | null) => void
}) {
  const [items, setItems] = useState<Deployment[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!projectId) return
    let alive = true
    setLoading(true)
    api
      .listProjectDeployments(projectId)
      .then((r) => {
        if (alive) setItems(r)
      })
      .catch((e: unknown) => {
        if (alive) {
          const msg =
            e instanceof ApiError
              ? `${e.status} ${JSON.stringify(e.body)}`
              : String(e)
          onError(msg)
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [projectId, onError])

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载中…
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        当前项目还没有部署记录。
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2 font-medium">Environment</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Started</th>
            <th className="px-3 py-2 font-medium">Completed</th>
            <th className="px-3 py-2 font-medium">Duration</th>
            <th className="px-3 py-2 font-medium">Deployed By</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {items.map((d) => (
            <tr key={d.id} className="hover:bg-slate-50">
              <td className="px-3 py-2">
                <div className="font-medium text-slate-800">
                  {d.environment}
                </div>
                {d.environment_type ? (
                  <div className="text-[10px] uppercase text-slate-500">
                    {d.environment_type}
                  </div>
                ) : null}
              </td>
              <td className="px-3 py-2">
                <span
                  className={[
                    'rounded px-1.5 py-0.5 text-[10px] uppercase',
                    DEPLOY_STATUS_BADGE[d.status] ??
                      'bg-slate-100 text-slate-700',
                  ].join(' ')}
                >
                  {d.status.toLowerCase()}
                </span>
                {d.error_message ? (
                  <div className="mt-1 max-w-xs truncate text-[10px] text-rose-600">
                    {d.error_message}
                  </div>
                ) : null}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {d.started_at ? new Date(d.started_at).toLocaleString() : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {d.completed_at
                  ? new Date(d.completed_at).toLocaleString()
                  : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {d.duration_ms != null ? `${d.duration_ms} ms` : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {d.deployed_by_name ?? '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ===== Audit Tab =====

function AuditTab({
  projectId,
  onError,
}: {
  projectId: string
  onError: (msg: string | null) => void
}) {
  const [items, setItems] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [typeFilter, setTypeFilter] = useState<string>('')

  useEffect(() => {
    if (!projectId) return
    let alive = true
    setLoading(true)
    api
      .listProjectAudit(projectId, 200)
      .then((r) => {
        if (alive) setItems(r)
      })
      .catch((e: unknown) => {
        if (alive) {
          const msg =
            e instanceof ApiError
              ? `${e.status} ${JSON.stringify(e.body)}`
              : String(e)
          onError(msg)
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [projectId, onError])

  const filtered = typeFilter
    ? items.filter((i) => i.event_type === typeFilter)
    : items

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载中…
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label className="text-xs text-slate-500">类型</label>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs"
        >
          <option value="">全部</option>
          {Object.keys(AUDIT_TYPE_BADGE).map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-500">
          共 {filtered.length} / {items.length} 条
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
          当前项目还没有审计事件。
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Actor</th>
                <th className="px-3 py-2 font-medium">Target</th>
                <th className="px-3 py-2 font-medium">Hash</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.map((e) => (
                <tr key={e.id} className="hover:bg-slate-50">
                  <td className="px-3 py-2 text-xs text-slate-600">
                    {new Date(e.created_at).toLocaleString()}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={[
                        'rounded px-1.5 py-0.5 text-[10px] uppercase',
                        AUDIT_TYPE_BADGE[e.event_type] ??
                          'bg-slate-100 text-slate-600',
                      ].join(' ')}
                    >
                      {e.event_type.toLowerCase()}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-700">
                    {e.actor_name ?? e.actor_id?.slice(0, 8) ?? '—'}
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-700">
                    <div className="font-mono text-[10px] uppercase text-slate-500">
                      {e.target_type ?? '—'}
                    </div>
                    <div className="truncate">
                      {e.target_label ?? e.target_id?.slice(0, 12) ?? '—'}
                    </div>
                  </td>
                  <td className="px-3 py-2 font-mono text-[10px] text-slate-400">
                    {e.entry_hash ? `${e.entry_hash.slice(0, 8)}…` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
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
      <dt className="w-32 shrink-0 text-slate-500">{label}</dt>
      <dd
        className={[
          'flex-1 text-slate-800',
          mono ? 'font-mono text-xs' : '',
        ].join(' ')}
      >
        {value}
      </dd>
    </div>
  )
}
