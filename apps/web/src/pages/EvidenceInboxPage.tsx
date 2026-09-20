import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  AlertTriangle,
  Check,
  Loader2,
  RefreshCw,
  Sparkles,
  UploadCloud,
  X,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type {
  CandidateProfileItem,
  Evidence,
  Proposal,
  ProposalStatus,
} from '../lib/types'
import { PageHeader } from '../components/PageHeader'

const PII_FIELD_HINTS = [
  'email',
  'phone',
  'mobile',
  'id_card',
  '身份证',
  'ssn',
  'name',
  '姓名',
  '地址',
  'address',
  '银行卡',
  'bank',
]

function looksLikePII(field: string): boolean {
  const lower = field.toLowerCase()
  return PII_FIELD_HINTS.some((h) => lower.includes(h.toLowerCase()))
}

function confidenceBadge(level: string | undefined): string {
  switch ((level ?? '').toLowerCase()) {
    case 'high':
    case '高':
      return 'bg-emerald-100 text-emerald-700'
    case 'medium':
    case '中':
      return 'bg-amber-100 text-amber-700'
    case 'low':
    case '低':
      return 'bg-rose-100 text-rose-700'
    default:
      return 'bg-slate-100 text-slate-600'
  }
}

function statusBadge(status: ProposalStatus): string {
  switch (status) {
    case 'PENDING':
      return 'bg-slate-100 text-slate-700'
    case 'ACCEPTED':
      return 'bg-emerald-100 text-emerald-700'
    case 'REJECTED':
      return 'bg-rose-100 text-rose-700'
    case 'MERGED':
      return 'bg-sky-100 text-sky-700'
    case 'SUPERSEDED':
      return 'bg-slate-200 text-slate-500'
  }
}

export function EvidenceInboxPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const pid = projectId ?? ''

  const [evidences, setEvidences] = useState<Evidence[]>([])
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const [selectedField, setSelectedField] = useState<string | null>(null)
  const [profiles, setProfiles] = useState<CandidateProfileItem[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    if (!pid) return
    setLoading(true)
    setError(null)
    try {
      const [ev, pr] = await Promise.all([
        api.listProjectEvidences(pid),
        api.listProposals(pid),
      ])
      setEvidences(ev)
      setProposals(pr)
      if (!selectedEvidenceId && ev.length > 0) {
        setSelectedEvidenceId(ev[0].id)
      }
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [pid, selectedEvidenceId])

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

  // 重新生成候选 → 调后端 from-evidence → 重新拉 proposals
  const regenerate = useCallback(async () => {
    if (!pid) return
    setGenerating(true)
    setError(null)
    setInfo(null)
    try {
      const resp = await api.generateCandidatesFromEvidence(pid)
      setProfiles(resp.field_profiles)
      setInfo(
        `已生成 ${resp.proposals_created} 个候选（跳过 ${resp.proposals_skipped}），共剖析 ${resp.field_profiles.length} 个字段。`,
      )
      // refresh proposals list
      const pr = await api.listProposals(pid)
      setProposals(pr)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
      setError(msg)
    } finally {
      setGenerating(false)
    }
  }, [pid])

  // 上传文件
  const handleFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0 || !pid) return
      setUploading(true)
      setError(null)
      setInfo(null)
      try {
        const results: string[] = []
        for (const file of Array.from(files)) {
          const r = await api.uploadEvidence(pid, file)
          results.push(r.evidence.id)
        }
        setInfo(`已上传 ${results.length} 个文件。`)
        await refresh()
      } catch (e: unknown) {
        const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
        setError(msg)
      } finally {
        setUploading(false)
      }
    },
    [pid, refresh],
  )

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault()
      setDragOver(false)
      void handleFiles(e.dataTransfer.files)
    },
    [handleFiles],
  )

  // 单条决策
  const decide = useCallback(
    async (proposalId: string, decision: 'accept' | 'reject') => {
      setBusyIds((s) => new Set(s).add(proposalId))
      setError(null)
      try {
        await api.decideProposal(proposalId, { decision })
        setProposals((arr) =>
          arr.map((p) =>
            p.id === proposalId
              ? { ...p, status: decision === 'accept' ? 'ACCEPTED' : 'REJECTED' }
              : p,
          ),
        )
      } catch (e: unknown) {
        const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
        setError(msg)
      } finally {
        setBusyIds((s) => {
          const ns = new Set(s)
          ns.delete(proposalId)
          return ns
        })
      }
    },
    [],
  )

  // 当前 evidence 的 proposal 子集 — 通过 field_name 前缀匹配（Proposal.title 通常以字段名开头）
  const currentProposals = useMemo(() => {
    if (!selectedEvidenceId) return []
    const ev = evidences.find((e) => e.id === selectedEvidenceId)
    const fieldName = ev?.field_name?.trim()
    if (fieldName) {
      return proposals.filter((p) =>
        p.title.toLowerCase().startsWith(fieldName.toLowerCase()),
      )
    }
    // 没有 field_name 的 evidence（如 SOURCE_RECORD）→ 显示所有 evidence 来源的提案
    return proposals.filter((p) => p.source === 'evidence')
  }, [proposals, selectedEvidenceId, evidences])

  const currentEvidence = useMemo(
    () => evidences.find((e) => e.id === selectedEvidenceId) ?? null,
    [evidences, selectedEvidenceId],
  )

  const currentProfile = useMemo(
    () => (selectedField ? profiles.find((p) => p.field_name === selectedField) : null),
    [profiles, selectedField],
  )

  // 批量决策（当前 evidence 下所有 PENDING）
  const batchDecide = useCallback(
    async (decision: 'accept' | 'reject') => {
      if (!pid || !selectedEvidenceId) return
      const ids = currentProposals
        .filter((p) => p.status === 'PENDING')
        .map((p) => p.id)
      if (ids.length === 0) {
        setInfo('当前没有可批量处理的 PENDING 提案。')
        return
      }
      setError(null)
      try {
        const r = await api.batchReviewProposals(ids, decision)
        setInfo(
          `批量${decision === 'accept' ? '接受' : '拒绝'}完成：${r.accepted}/${r.total} 接受，${r.rejected}/${r.total} 拒绝，${r.skipped} 跳过。`,
        )
        await refresh()
      } catch (e: unknown) {
        const msg = e instanceof ApiError ? `${e.status} ${JSON.stringify(e.body)}` : String(e)
        setError(msg)
      }
    },
    [pid, selectedEvidenceId, currentProposals, refresh],
  )

  return (
    <PageHeader
      title="证据收件箱"
      description="上传数据样本，查看自动生成的候选映射并决策。"
      actions={
        <button
          type="button"
          onClick={regenerate}
          disabled={generating || evidences.length === 0}
          className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
          {generating ? '生成中…' : '从证据生成候选'}
        </button>
      }
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

      {/* Upload zone */}
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        className={[
          'mb-6 flex items-center justify-center rounded-lg border-2 border-dashed p-6 transition',
          dragOver
            ? 'border-sky-400 bg-sky-50'
            : 'border-slate-300 bg-white hover:border-slate-400',
        ].join(' ')}
      >
        <div className="text-center">
          <UploadCloud size={28} className="mx-auto mb-2 text-slate-400" />
          <p className="text-sm text-slate-700">
            拖拽文件到此处，或
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="ml-1 font-medium text-sky-600 hover:text-sky-700 disabled:opacity-50"
            >
              {uploading ? '上传中…' : '点击选择'}
            </button>
          </p>
          <p className="mt-1 text-xs text-slate-500">
            支持 CSV / XLSX / JSON / Markdown / 文本，最大 50 MB
          </p>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".csv,.xlsx,.xls,.json,.txt,.md,.markdown"
            className="hidden"
            onChange={(e) => void handleFiles(e.target.files)}
          />
        </div>
      </div>

      {loading ? (
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
          加载中…
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
          {/* Evidence list */}
          <aside className="rounded-lg border border-slate-200 bg-white">
            <header className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
              <h2 className="text-sm font-semibold text-slate-800">
                证据 ({evidences.length})
              </h2>
              <button
                type="button"
                onClick={() => void refresh()}
                className="rounded p-1 text-slate-500 hover:bg-slate-100"
                title="刷新"
              >
                <RefreshCw size={14} />
              </button>
            </header>
            {evidences.length === 0 ? (
              <div className="p-4 text-sm text-slate-500">
                还没有证据，先上传一个 CSV / Excel 文件试试。
              </div>
            ) : (
              <ul className="divide-y divide-slate-100">
                {evidences.map((e) => (
                  <li key={e.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedEvidenceId(e.id)
                        setSelectedField(null)
                      }}
                      className={[
                        'block w-full px-3 py-2 text-left text-sm hover:bg-slate-50',
                        selectedEvidenceId === e.id ? 'bg-sky-50' : '',
                      ].join(' ')}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs text-slate-700">
                          {e.field_name ?? e.location ?? e.id.slice(0, 8)}
                        </span>
                        <span
                          className={[
                            'shrink-0 rounded-full px-1.5 py-0.5 text-[10px] uppercase',
                            e.is_confirmed
                              ? 'bg-emerald-100 text-emerald-700'
                              : 'bg-slate-100 text-slate-600',
                          ].join(' ')}
                        >
                          {e.is_confirmed ? '已确认' : e.strength}
                        </span>
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-slate-500">
                        {e.evidence_type} · {new Date(e.created_at).toLocaleString()}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </aside>

          {/* Main: proposals table + detail */}
          <section className="space-y-4">
            {currentEvidence ? (
              <div className="rounded-lg border border-slate-200 bg-white">
                <header className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
                  <div>
                    <h3 className="text-sm font-semibold text-slate-800">
                      候选映射（基于当前证据）
                    </h3>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {currentProposals.length} 条提案
                      {currentProposals.filter((p) => p.status === 'PENDING').length >
                        0 &&
                        `，其中 ${currentProposals.filter((p) => p.status === 'PENDING').length} 条待决策`}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => void batchDecide('accept')}
                      disabled={
                        currentProposals.filter((p) => p.status === 'PENDING').length ===
                        0
                      }
                      className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
                    >
                      <Check size={12} />
                      批量接受
                    </button>
                    <button
                      type="button"
                      onClick={() => void batchDecide('reject')}
                      disabled={
                        currentProposals.filter((p) => p.status === 'PENDING').length ===
                        0
                      }
                      className="flex items-center gap-1.5 rounded-md border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <X size={12} />
                      批量拒绝
                    </button>
                  </div>
                </header>

                {currentProposals.length === 0 ? (
                  <div className="p-6 text-sm text-slate-500">
                    当前证据下还没有候选。点击右上「从证据生成候选」自动生成。
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                        <tr>
                          <th className="px-3 py-2 font-medium">source_field</th>
                          <th className="px-3 py-2 font-medium">target_object</th>
                          <th className="px-3 py-2 font-medium">target_property</th>
                          <th className="px-3 py-2 font-medium">type</th>
                          <th className="px-3 py-2 font-medium">confidence</th>
                          <th className="px-3 py-2 font-medium">status</th>
                          <th className="px-3 py-2 font-medium text-right">action</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {currentProposals.map((p) => {
                          const field = p.title.split(/[:.>]/)[0]?.trim() ?? p.title
                          const isSel = selectedField === field
                          const busy = busyIds.has(p.id)
                          return (
                            <tr
                              key={p.id}
                              onClick={() => setSelectedField(field)}
                              className={[
                                'cursor-pointer hover:bg-slate-50',
                                isSel ? 'bg-sky-50' : '',
                              ].join(' ')}
                            >
                              <td className="px-3 py-2 font-mono text-xs">
                                <span className="inline-flex items-center gap-1">
                                  {field}
                                  {looksLikePII(field) ? (
                                    <span title="疑似 PII 字段">
                                      <AlertTriangle
                                        size={12}
                                        className="text-amber-500"
                                      />
                                    </span>
                                  ) : null}
                                </span>
                              </td>
                              <td className="px-3 py-2 text-xs text-slate-700">
                                {p.suggested_iri ? (
                                  <span className="font-mono">
                                    {p.suggested_iri.split('#').pop() ??
                                      p.suggested_iri}
                                  </span>
                                ) : (
                                  <span className="text-slate-400">—</span>
                                )}
                              </td>
                              <td className="px-3 py-2 text-xs text-slate-700">
                                {p.description ?? (
                                  <span className="text-slate-400">—</span>
                                )}
                              </td>
                              <td className="px-3 py-2 text-xs uppercase text-slate-500">
                                {p.proposal_type.toLowerCase()}
                              </td>
                              <td className="px-3 py-2 text-xs">
                                <span
                                  className={[
                                    'rounded px-1.5 py-0.5 text-[10px] uppercase',
                                    confidenceBadge(p.confidence),
                                  ].join(' ')}
                                >
                                  {p.confidence.toLowerCase()}
                                </span>
                                {p.confidence_score != null ? (
                                  <span className="ml-1 text-slate-400">
                                    {(p.confidence_score * 100).toFixed(0)}%
                                  </span>
                                ) : null}
                              </td>
                              <td className="px-3 py-2 text-xs">
                                <span
                                  className={[
                                    'rounded px-1.5 py-0.5 text-[10px] uppercase',
                                    statusBadge(p.status),
                                  ].join(' ')}
                                >
                                  {p.status.toLowerCase()}
                                </span>
                              </td>
                              <td className="px-3 py-2 text-right">
                                {p.status === 'PENDING' ? (
                                  <div className="inline-flex gap-1">
                                    <button
                                      type="button"
                                      disabled={busy}
                                      onClick={(ev) => {
                                        ev.stopPropagation()
                                        void decide(p.id, 'accept')
                                      }}
                                      className="rounded bg-emerald-600 px-2 py-0.5 text-xs text-white hover:bg-emerald-700 disabled:opacity-50"
                                    >
                                      接受
                                    </button>
                                    <button
                                      type="button"
                                      disabled={busy}
                                      onClick={(ev) => {
                                        ev.stopPropagation()
                                        void decide(p.id, 'reject')
                                      }}
                                      className="rounded border border-rose-300 bg-white px-2 py-0.5 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                                    >
                                      拒绝
                                    </button>
                                  </div>
                                ) : (
                                  <span className="text-xs text-slate-400">—</span>
                                )}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ) : (
              <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
                左侧选择一条证据查看候选。
              </div>
            )}

            {/* Detail panel */}
            {selectedField && currentProfile ? (
              <div className="rounded-lg border border-slate-200 bg-white p-4">
                <h3 className="text-sm font-semibold text-slate-800">
                  字段详情：<span className="font-mono">{selectedField}</span>
                </h3>
                <dl className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-slate-500">
                      推断类型
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {currentProfile.inferred_type}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-slate-500">
                      置信度
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {(currentProfile.confidence * 100).toFixed(0)}%
                      <span
                        className={[
                          'ml-2 rounded px-1.5 py-0.5 text-[10px] uppercase',
                          confidenceBadge(currentProfile.confidence_level),
                        ].join(' ')}
                      >
                        {String(currentProfile.confidence_level).toLowerCase()}
                      </span>
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs uppercase tracking-wide text-slate-500">
                      PII 提示
                    </dt>
                    <dd className="mt-1">
                      {looksLikePII(selectedField) ? (
                        <span className="inline-flex items-center gap-1 rounded bg-amber-50 px-2 py-0.5 text-xs text-amber-700">
                          <AlertTriangle size={12} />
                          字段名命中 PII 关键词，注意脱敏
                        </span>
                      ) : (
                        <span className="text-xs text-slate-500">未识别为 PII</span>
                      )}
                    </dd>
                  </div>
                </dl>
              </div>
            ) : null}
          </section>
        </div>
      )}
    </PageHeader>
  )
}
