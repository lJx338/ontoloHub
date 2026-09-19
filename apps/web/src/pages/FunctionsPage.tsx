import { useEffect, useMemo, useState } from 'react'
import { Loader2, Play, Plus, Save, Search, Trash2 } from 'lucide-react'
import { PageHeader } from '../components/PageHeader'
import { ProjectSelector } from '../components/ProjectSelector'
import { CodeEditor, type CodeEditorHandle } from '../components/CodeEditor'
import { JsonView } from '../components/JsonView'
import { ApiError, api } from '../lib/api'
import type { FunctionLanguage, FunctionRecord, FunctionRunRecord, FunctionTestResponse } from '../lib/types'

const DEFAULT_PYTHON = `def handler(input_data, context):
    """Return a greeting. 'input_data' is whatever the caller passes."""
    name = (input_data or {}).get("name", "world")
    return {"greeting": f"hello, {name}"}
`

const DEFAULT_JAVASCRIPT = `async function handler(input_data, context) {
  const name = (input_data && input_data.name) || "world";
  return { greeting: \`hello, \${name}\` };
}
`

const STARTER_TEMPLATE = (language: FunctionLanguage): string => {
  if (language === 'javascript') return DEFAULT_JAVASCRIPT
  return DEFAULT_PYTHON
}

export function FunctionsPage() {
  const [projectId, setProjectId] = useState<string | null>(null)
  const [functions, setFunctions] = useState<FunctionRecord[]>([])
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 当前编辑中的 Function 草稿（独立于服务端返回，避免受控输入卡顿）
  const [draft, setDraft] = useState<FunctionRecord | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  // Test runner
  const [testInput, setTestInput] = useState<string>('{\n  "name": "OntoloHub"\n}')
  const [testResult, setTestResult] = useState<FunctionTestResponse | null>(null)
  const [testing, setTesting] = useState(false)
  const [runs, setRuns] = useState<FunctionRunRecord[]>([])

  const filtered = useMemo(
    () =>
      functions.filter((f) =>
        search ? `${f.api_name} ${f.display_name}`.toLowerCase().includes(search.toLowerCase()) : true,
      ),
    [functions, search],
  )

  // ---- 拉取列表 ----
  const reloadFunctions = async (pid: string) => {
    setLoading(true)
    setError(null)
    try {
      const list = await api.get<FunctionRecord[]>(`/projects/${pid}/functions`)
      setFunctions(list)
      // 选择仍在列表中的项，否则选第一个
      const stillValid = list.find((f) => f.id === selectedId)
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
    if (projectId) void reloadFunctions(projectId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  // ---- 创建 ----
  const createFunction = async () => {
    if (!projectId) return
    const api_name = window.prompt('Function api_name（字母/下划线开头，字母数字下划线点）')?.trim()
    if (!api_name) return
    const display_name = window.prompt('显示名称')?.trim() || api_name
    const language: FunctionLanguage = (window.confirm('使用 JavaScript？点「取消」选 Python') ? 'javascript' : 'python')
    try {
      const created = await api.post<FunctionRecord>(`/projects/${projectId}/functions`, {
        api_name,
        display_name,
        language,
        source_code: STARTER_TEMPLATE(language),
      })
      await reloadFunctions(projectId)
      setSelectedId(created.id)
      setDraft(created)
      setDirty(false)
    } catch (e) {
      const msg =
        e instanceof ApiError && e.status === 409
          ? `api_name '${api_name}' 已存在`
          : e instanceof Error
          ? e.message
          : String(e)
      window.alert(`创建失败：${msg}`)
    }
  }

  // ---- 删除 ----
  const deleteFunction = async (fn: FunctionRecord) => {
    if (!projectId) return
    if (!window.confirm(`确认删除 ${fn.api_name}（version ${fn.version}）？此操作不可撤销。`)) return
    try {
      await api.delete(`/projects/${projectId}/functions/${fn.id}`)
      setSelectedId(null)
      setDraft(null)
      await reloadFunctions(projectId)
    } catch (e) {
      window.alert(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // ---- 保存 ----
  const saveDraft = async () => {
    if (!projectId || !draft) return
    setSaving(true)
    try {
      const updated = await api.patch<FunctionRecord>(
        `/projects/${projectId}/functions/${draft.id}`,
        {
          api_name: draft.api_name,
          display_name: draft.display_name,
          description: draft.description,
          source_code: draft.source_code,
          parameters_schema: draft.parameters_schema,
          return_schema: draft.return_schema,
          config: draft.config,
        },
      )
      setDraft({ ...updated })
      setDirty(false)
      await reloadFunctions(projectId)
    } catch (e) {
      window.alert(`保存失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  // ---- 测试运行 ----
  const runTest = async () => {
    if (!projectId || !draft) return
    let parsedInput: unknown
    try {
      parsedInput = testInput.trim() ? JSON.parse(testInput) : {}
    } catch (e) {
      window.alert(`测试输入不是合法 JSON：${e instanceof Error ? e.message : String(e)}`)
      return
    }
    setTesting(true)
    setTestResult(null)
    try {
      const res = await api.post<FunctionTestResponse>(
        `/projects/${projectId}/functions/${draft.id}/test`,
        { input_data: parsedInput, timeout_s: 30 },
      )
      setTestResult(res)
      // 拉取最近的 runs
      const recent = await api.get<FunctionRunRecord[]>(
        `/projects/${projectId}/functions/${draft.id}/runs`,
        { limit: 10 },
      )
      setRuns(recent)
    } catch (e) {
      setTestResult({
        run_id: '00000000-0000-0000-0000-000000000000',
        function_id: draft.id,
        version: draft.version,
        output_data: null,
        stdout: null,
        stderr: null,
        error: e instanceof Error ? e.message : String(e),
        duration_ms: 0,
        timed_out: false,
      })
    } finally {
      setTesting(false)
    }
  }

  // 切换选中 → 同步拉 runs
  useEffect(() => {
    if (!projectId || !selectedId) {
      setRuns([])
      return
    }
    api
      .get<FunctionRunRecord[]>(`/projects/${projectId}/functions/${selectedId}/runs`, { limit: 10 })
      .then(setRuns)
      .catch(() => setRuns([]))
  }, [projectId, selectedId])

  // ---- 渲染 ----
  return (
    <PageHeader
      title="Functions"
      description="可独立寻址的代码函数：被 ActionType 引用、被 Workflow 步骤调用，可在编辑器内直接试运行。"
      actions={<ProjectSelector value={projectId} onChange={setProjectId} />}
    >
      {!projectId ? (
        <EmptyHint>请先选择一个项目以查看其 Function。</EmptyHint>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_1fr]">
          {/* 左：列表 */}
          <aside className="flex flex-col rounded-lg border border-slate-200 bg-white">
            <div className="flex items-center gap-2 border-b border-slate-200 p-2">
              <div className="relative flex-1">
                <Search size={14} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜索 api_name / display"
                  className="w-full rounded-md border border-slate-300 bg-white py-1.5 pl-7 pr-2 text-sm focus:border-sky-500 focus:outline-none"
                />
              </div>
              <button
                onClick={createFunction}
                className="flex items-center gap-1 rounded-md bg-sky-600 px-2 py-1.5 text-xs font-medium text-white hover:bg-sky-700"
                title="新建 Function"
              >
                <Plus size={14} /> 新建
              </button>
            </div>
            <div className="flex-1 overflow-auto">
              {loading ? (
                <div className="flex items-center justify-center p-6 text-xs text-slate-500">
                  <Loader2 size={14} className="mr-1 animate-spin" /> 加载中…
                </div>
              ) : error ? (
                <div className="p-3 text-xs text-rose-600">错误：{error}</div>
              ) : filtered.length === 0 ? (
                <div className="p-6 text-center text-xs text-slate-500">
                  暂无 Function。点击「新建」开始。
                </div>
              ) : (
                <ul className="divide-y divide-slate-100">
                  {filtered.map((f) => (
                    <li
                      key={f.id}
                      className={[
                        'flex cursor-pointer items-start justify-between gap-2 px-3 py-2 text-sm hover:bg-slate-50',
                        selectedId === f.id ? 'bg-sky-50' : '',
                      ].join(' ')}
                      onClick={() => {
                        setSelectedId(f.id)
                        setDraft({ ...f })
                        setDirty(false)
                        setTestResult(null)
                      }}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-medium text-slate-900">{f.display_name}</div>
                        <div className="truncate font-mono text-xs text-slate-500">
                          {f.api_name} · v{f.version} · {f.language}
                        </div>
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          void deleteFunction(f)
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

          {/* 右：编辑器 + 测试 */}
          <section className="flex flex-col gap-4">
            {!draft ? (
              <EmptyHint>左侧选择一个 Function 来编辑，或点击「新建」。</EmptyHint>
            ) : (
              <>
                {/* 头部 */}
                <div className="rounded-lg border border-slate-200 bg-white p-4">
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <Field label="显示名称">
                      <input
                        value={draft.display_name}
                        onChange={(e) => {
                          setDraft({ ...draft, display_name: e.target.value })
                          setDirty(true)
                        }}
                        className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none"
                      />
                    </Field>
                    <Field label="api_name">
                      <input
                        value={draft.api_name}
                        onChange={(e) => {
                          setDraft({ ...draft, api_name: e.target.value })
                          setDirty(true)
                        }}
                        className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 font-mono text-sm focus:border-sky-500 focus:outline-none"
                      />
                    </Field>
                    <Field label="描述">
                      <input
                        value={draft.description ?? ''}
                        onChange={(e) => {
                          setDraft({ ...draft, description: e.target.value || null })
                          setDirty(true)
                        }}
                        className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-sky-500 focus:outline-none"
                      />
                    </Field>
                    <Field label="语言 / 版本">
                      <div className="flex items-center gap-2 text-sm text-slate-700">
                        <span className="rounded bg-slate-100 px-2 py-1 font-mono text-xs">{draft.language}</span>
                        <span className="rounded bg-sky-100 px-2 py-1 font-mono text-xs text-sky-700">v{draft.version}</span>
                        <span className="ml-auto text-xs text-slate-500">
                          {dirty ? '● 未保存' : '已同步'}
                        </span>
                      </div>
                    </Field>
                  </div>
                </div>

                {/* 代码编辑器 */}
                <div className="rounded-lg border border-slate-200 bg-white p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-sm font-medium text-slate-700">源代码</h3>
                    <button
                      onClick={saveDraft}
                      disabled={!dirty || saving}
                      className="flex items-center gap-1 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                      保存
                    </button>
                  </div>
                  <CodeEditor
                    value={draft.source_code}
                    onChange={(next) => {
                      setDraft({ ...draft, source_code: next })
                      setDirty(true)
                    }}
                    language={draft.language}
                    minRows={16}
                    aria-label="Function 源代码"
                  />
                  <p className="mt-2 text-xs text-slate-500">
                    Python 入口 <code className="font-mono">handler(input_data, context)</code>；JavaScript 同名。
                    返回值（dict）会作为 <code className="font-mono">output_data</code>。
                  </p>
                </div>

                {/* Test runner */}
                <div className="rounded-lg border border-slate-200 bg-white p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-sm font-medium text-slate-700">试运行</h3>
                    <button
                      onClick={runTest}
                      disabled={testing}
                      className="flex items-center gap-1 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
                    >
                      {testing ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                      Run
                    </button>
                  </div>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <div>
                      <div className="mb-1 text-xs font-medium text-slate-600">input_data (JSON)</div>
                      <CodeEditor
                        value={testInput}
                        onChange={setTestInput}
                        language="json"
                        minRows={8}
                        aria-label="测试输入 JSON"
                      />
                    </div>
                    <div>
                      <div className="mb-1 text-xs font-medium text-slate-600">最近结果</div>
                      {testResult ? (
                        <div className="space-y-2">
                          <ResultLine label="耗时" value={`${testResult.duration_ms} ms`} />
                          <ResultLine label="timed_out" value={String(testResult.timed_out)} />
                          <ResultLine
                            label="error"
                            value={testResult.error ?? '—'}
                            tone={testResult.error ? 'rose' : 'slate'}
                          />
                          <div>
                            <div className="text-xs font-medium text-slate-600">output_data</div>
                            <JsonView
                              value={testResult.output_data}
                              defaultExpanded
                              emptyText="（无返回值）"
                            />
                          </div>
                          {testResult.stdout && (
                            <div>
                              <div className="text-xs font-medium text-slate-600">stdout</div>
                              <pre className="overflow-auto rounded-md border border-slate-200 bg-slate-50 p-2 font-mono text-xs">
                                {testResult.stdout}
                              </pre>
                            </div>
                          )}
                          {testResult.stderr && (
                            <div>
                              <div className="text-xs font-medium text-slate-600">stderr</div>
                              <pre className="overflow-auto rounded-md border border-rose-200 bg-rose-50 p-2 font-mono text-xs text-rose-700">
                                {testResult.stderr}
                              </pre>
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="rounded-md border border-dashed border-slate-300 p-6 text-center text-xs text-slate-500">
                          点击「Run」执行
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* 历史 */}
                {runs.length > 0 && (
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <h3 className="mb-2 text-sm font-medium text-slate-700">最近试运行</h3>
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
                              <span
                                className={[
                                  'rounded px-1.5 py-0.5 font-mono',
                                  r.error ? 'bg-rose-100 text-rose-700' : 'bg-emerald-100 text-emerald-700',
                                ].join(' ')}
                              >
                                {r.error ? 'failed' : 'ok'}
                              </span>
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      {children}
    </label>
  )
}

function ResultLine({ label, value, tone = 'slate' }: { label: string; value: string; tone?: 'slate' | 'rose' }) {
  const cls = tone === 'rose' ? 'text-rose-700' : 'text-slate-700'
  return (
    <div className="flex items-baseline gap-2 text-xs">
      <span className="text-slate-500">{label}:</span>
      <span className={`font-mono ${cls}`}>{value}</span>
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

// 抑制未使用导入告警（handle 暂未暴露给上层用，保留 import 为未来插入 API）
export type { CodeEditorHandle }
