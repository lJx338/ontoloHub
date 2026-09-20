import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  AlertTriangle,
  ChevronRight,
  GitBranch,
  GitFork,
  GitMerge,
  Loader2,
  PencilLine,
  Plus,
  Trash2,
} from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type {
  ClassType,
  Ontology,
  OntologyClass,
  OntologyDiff,
  OntologyProperty,
  OntologyRelation,
  OntologyVersion,
  PropertyType,
} from '../lib/types'
import { PageHeader } from '../components/PageHeader'

type Tab = 'head' | 'versions' | 'diff'

const CLASS_TYPE_BADGE: Record<ClassType, string> = {
  ONTOLOGY_CLASS: 'bg-sky-100 text-sky-700',
  VALUE_RESTRICTION: 'bg-amber-100 text-amber-700',
  ENUMERATION: 'bg-violet-100 text-violet-700',
  UNION: 'bg-emerald-100 text-emerald-700',
}

const PROPERTY_TYPE_BADGE: Record<PropertyType, string> = {
  DATATYPE_PROPERTY: 'bg-slate-100 text-slate-700',
  OBJECT_PROPERTY: 'bg-violet-100 text-violet-700',
  ANNOTATION_PROPERTY: 'bg-amber-100 text-amber-700',
}

const DIFF_BADGE: Record<string, string> = {
  added: 'bg-emerald-100 text-emerald-700',
  removed: 'bg-rose-100 text-rose-700',
  modified: 'bg-amber-100 text-amber-700',
  unchanged: 'bg-slate-100 text-slate-500',
}

export function OntologyEditorPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const pid = projectId ?? ''
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('head')
  const [ontology, setOntology] = useState<Ontology | null>(null)
  const [loadingOntology, setLoadingOntology] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  // load ontology for project
  useEffect(() => {
    if (!pid) return
    let alive = true
    setLoadingOntology(true)
    setError(null)
    api
      .listOntologies(pid)
      .then((list) => {
        if (!alive) return
        if (list.length === 0) {
          setError('当前项目还没有本体')
          setOntology(null)
          return
        }
        setOntology(list[0])
      })
      .catch((e: unknown) => {
        if (alive) onErr(e, setError)
      })
      .finally(() => {
        if (alive) setLoadingOntology(false)
      })
    return () => {
      alive = false
    }
  }, [pid])

  const handlePublish = useCallback(async () => {
    if (!ontology) return
    setError(null)
    setInfo(null)
    try {
      const r = await api.publishOntology(ontology.id)
      setInfo(`已发布版本 ${r.version}。`)
    } catch (e: unknown) {
      onErr(e, setError)
    }
  }, [ontology])

  const handleFork = useCallback(async () => {
    if (!ontology) return
    const newName = window.prompt(
      'Fork 新本体名称',
      `${ontology.name}-fork`,
    )
    if (!newName) return
    setError(null)
    setInfo(null)
    try {
      const r = await api.forkOntology(ontology.id, { new_name: newName })
      setInfo(`已 fork 为 ${r.forked_ontology_name}。`)
    } catch (e: unknown) {
      onErr(e, setError)
    }
  }, [ontology])

  if (loadingOntology) {
    return (
      <PageHeader title="本体编辑器" description="加载中…">
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
          加载本体…
        </div>
      </PageHeader>
    )
  }
  if (!ontology) {
    return (
      <PageHeader title="本体编辑器" description="">
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700">
          {error ?? '未找到本体。'}
        </div>
      </PageHeader>
    )
  }

  return (
    <PageHeader
      title={`本体 — ${ontology.name}`}
      description={`namespace: ${ontology.namespace} · ${ontology.class_count} 类 / ${ontology.property_count} 属性`}
      actions={
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void handlePublish()}
            className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700"
          >
            <GitMerge size={12} />
            保存草稿 / 发布
          </button>
          <button
            type="button"
            onClick={() =>
              navigate(`/projects/${pid}/changes?from_ontology=${ontology.id}`)
            }
            className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <GitBranch size={12} />
            创建 Change Request
          </button>
          <button
            type="button"
            onClick={() => void handleFork()}
            className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            <GitFork size={12} />
            Fork 新版本
          </button>
        </div>
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

      <div className="mb-4 border-b border-slate-200">
        <nav className="flex gap-1" aria-label="tabs">
          <TabBtn active={tab === 'head'} onClick={() => setTab('head')} label="当前 head" />
          <TabBtn
            active={tab === 'versions'}
            onClick={() => setTab('versions')}
            label="所有版本"
          />
          <TabBtn
            active={tab === 'diff'}
            onClick={() => setTab('diff')}
            label="Diff 视图"
          />
        </nav>
      </div>

      {tab === 'head' ? (
        <EditorTab
          ontologyId={ontology.id}
          onError={setError}
          onInfo={setInfo}
        />
      ) : tab === 'versions' ? (
        <VersionsTab ontologyId={ontology.id} onError={setError} />
      ) : (
        <DiffTab ontologyId={ontology.id} onError={setError} />
      )}
    </PageHeader>
  )
}

function TabBtn({
  active,
  onClick,
  label,
}: {
  active: boolean
  onClick: () => void
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        '-mb-px border-b-2 px-4 py-2 text-sm transition',
        active
          ? 'border-sky-500 font-medium text-sky-700'
          : 'border-transparent text-slate-500 hover:text-slate-700',
      ].join(' ')}
    >
      {label}
    </button>
  )
}

// ===== Editor Tab =====

function EditorTab({
  ontologyId,
  onError,
  onInfo,
}: {
  ontologyId: string
  onError: (msg: string | null) => void
  onInfo: (msg: string | null) => void
}) {
  const [classes, setClasses] = useState<OntologyClass[]>([])
  const [properties, setProperties] = useState<OntologyProperty[]>([])
  const [relations, setRelations] = useState<OntologyRelation[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedClassId, setSelectedClassId] = useState<string | null>(null)
  const [selectedPropertyId, setSelectedPropertyId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    onError(null)
    try {
      const [c, p, r] = await Promise.all([
        api.listOntologyClasses(ontologyId),
        api.listOntologyProperties(ontologyId),
        api.listOntologyRelations(ontologyId),
      ])
      setClasses(c)
      setProperties(p)
      setRelations(r)
      if (!selectedClassId && c.length > 0) setSelectedClassId(c[0].id)
    } catch (e: unknown) {
      onErr(e, onError)
    } finally {
      setLoading(false)
    }
  }, [ontologyId, selectedClassId, onError])

  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ontologyId])

  const selectedClass = useMemo(
    () => classes.find((c) => c.id === selectedClassId) ?? null,
    [classes, selectedClassId],
  )
  const selectedProperty = useMemo(
    () => properties.find((p) => p.id === selectedPropertyId) ?? null,
    [properties, selectedPropertyId],
  )

  const classProperties = useMemo(
    () =>
      selectedClass
        ? properties.filter((p) => p.domain_iri === selectedClass.iri)
        : [],
    [properties, selectedClass],
  )
  const classRelations = useMemo(
    () =>
      selectedClass
        ? relations.filter(
            (r) =>
              r.source_class_iri === selectedClass.iri ||
              r.target_class_iri === selectedClass.iri,
          )
        : [],
    [relations, selectedClass],
  )

  // build tree from classes (using parent_iri)
  const tree = useMemo(() => buildClassTree(classes), [classes])

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载本体…
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px_1fr_320px]">
      {/* Tree */}
      <aside className="rounded-lg border border-slate-200 bg-white">
        <header className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
          <h3 className="text-sm font-semibold text-slate-800">
            对象类型 ({classes.length})
          </h3>
          <button
            type="button"
            onClick={async () => {
              const name = window.prompt('新类名', 'NewClass')
              if (!name) return
              const iri = window.prompt('IRI', `urn:local:${name}`)
              if (!iri) return
              setBusy(true)
              try {
                const created = await api.createOntologyClass(ontologyId, {
                  name,
                  iri,
                  parent_iri: selectedClass?.iri ?? undefined,
                })
                onInfo(`已创建类 ${created.name}。`)
                setSelectedClassId(created.id)
                await refresh()
              } catch (e) {
                onErr(e, onError)
              } finally {
                setBusy(false)
              }
            }}
            className="flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            disabled={busy}
          >
            <Plus size={11} />
            新建
          </button>
        </header>
        <ul className="max-h-[60vh] overflow-y-auto p-2 text-sm">
          {tree.map((node) => (
            <ClassTreeNode
              key={node.cls.id}
              node={node}
              selectedId={selectedClassId}
              onSelect={(id) => {
                setSelectedClassId(id)
                setSelectedPropertyId(null)
              }}
            />
          ))}
        </ul>
      </aside>

      {/* Property table */}
      <section className="rounded-lg border border-slate-200 bg-white">
        <header className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
          <div>
            <h3 className="text-sm font-semibold text-slate-800">
              {selectedClass ? selectedClass.name : '未选择类'}
            </h3>
            {selectedClass ? (
              <p className="mt-0.5 text-[11px] text-slate-500">
                {selectedClass.iri}
                {selectedClass.description
                  ? ` · ${selectedClass.description}`
                  : ''}
              </p>
            ) : null}
          </div>
          {selectedClass ? (
            <button
              type="button"
              onClick={async () => {
                const name = window.prompt('新属性名', 'newProperty')
                if (!name) return
                const iri = window.prompt('IRI', `urn:local:${name}`)
                if (!iri) return
                setBusy(true)
                try {
                  await api.createOntologyProperty(ontologyId, {
                    name,
                    iri,
                    domain_iri: selectedClass.iri,
                  })
                  onInfo(`已创建属性 ${name}。`)
                  await refresh()
                } catch (e) {
                  onErr(e, onError)
                } finally {
                  setBusy(false)
                }
              }}
              className="flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              disabled={busy}
            >
              <Plus size={11} />
              新建属性
            </button>
          ) : null}
        </header>
        {!selectedClass ? (
          <div className="p-6 text-sm text-slate-500">
            左侧选择一个对象类型查看其属性。
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">IRI</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Range</th>
                  <th className="px-3 py-2 font-medium">Required</th>
                  <th className="px-3 py-2 font-medium">Multi</th>
                  <th className="px-3 py-2 font-medium" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {classProperties.map((p) => (
                  <tr
                    key={p.id}
                    onClick={() => setSelectedPropertyId(p.id)}
                    className={[
                      'cursor-pointer hover:bg-slate-50',
                      selectedPropertyId === p.id ? 'bg-sky-50' : '',
                    ].join(' ')}
                  >
                    <td className="px-3 py-2 font-medium text-slate-800">
                      {p.name}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-700">
                      {p.iri}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={[
                          'rounded px-1.5 py-0.5 text-[10px] uppercase',
                          PROPERTY_TYPE_BADGE[p.property_type] ??
                            'bg-slate-100 text-slate-700',
                        ].join(' ')}
                      >
                        {p.property_type.toLowerCase()}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-700">
                      {p.range_class_iri ?? p.range_type ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {p.is_required ? '✓' : '—'}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {p.is_multivalued ? '✓' : '—'}
                    </td>
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        onClick={async (e) => {
                          e.stopPropagation()
                          if (!window.confirm(`删除属性 "${p.name}"?`)) return
                          setBusy(true)
                          try {
                            await api.deleteOntologyProperty(
                              ontologyId,
                              p.id,
                            )
                            onInfo('属性已删除。')
                            if (selectedPropertyId === p.id)
                              setSelectedPropertyId(null)
                            await refresh()
                          } catch (er) {
                            onErr(er, onError)
                          } finally {
                            setBusy(false)
                          }
                        }}
                        className="rounded-md p-1 text-rose-600 hover:bg-rose-50 disabled:opacity-50"
                        disabled={busy || p.is_locked}
                        title={
                          p.is_locked
                            ? '已锁定的属性不可删除'
                            : '删除属性'
                        }
                      >
                        <Trash2 size={12} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {classRelations.length > 0 ? (
              <div className="border-t border-slate-100 p-3">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  关系
                </h4>
                <ul className="mt-1 divide-y divide-slate-100 text-sm">
                  {classRelations.map((r) => (
                    <li key={r.id} className="flex items-center gap-2 py-1">
                      <span className="font-medium text-slate-800">
                        {r.name}
                      </span>
                      <span className="font-mono text-[11px] text-slate-500">
                        {r.source_class_iri ?? '?'} → {r.target_class_iri ?? '?'}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </section>

      {/* Detail form */}
      <aside className="rounded-lg border border-slate-200 bg-white">
        <header className="border-b border-slate-100 px-3 py-2">
          <h3 className="text-sm font-semibold text-slate-800">详情</h3>
        </header>
        {!selectedClass ? (
          <div className="p-4 text-sm text-slate-500">未选择类。</div>
        ) : selectedProperty ? (
          <PropertyDetailForm
            ontologyId={ontologyId}
            property={selectedProperty}
            onSaved={async () => {
              onInfo('属性已更新。')
              await refresh()
            }}
            onError={(m) => onError(m)}
          />
        ) : (
          <ClassDetailForm
            ontologyId={ontologyId}
            cls={selectedClass}
            onSaved={async () => {
              onInfo('类已更新。')
              await refresh()
            }}
            onDelete={async () => {
              setBusy(true)
              try {
                await api.deleteOntologyClass(ontologyId, selectedClass.id)
                onInfo('类已删除。')
                setSelectedClassId(null)
                await refresh()
              } catch (e) {
                onErr(e, onError)
              } finally {
                setBusy(false)
              }
            }}
            onError={(m) => onError(m)}
          />
        )}
      </aside>
    </div>
  )
}

// Tree node
type ClassNode = { cls: OntologyClass; children: ClassNode[] }

function buildClassTree(classes: OntologyClass[]): ClassNode[] {
  const byIri = new Map<string, ClassNode>()
  for (const c of classes) byIri.set(c.iri, { cls: c, children: [] })
  const roots: ClassNode[] = []
  for (const c of classes) {
    const node = byIri.get(c.iri)!
    const parent = c.parent_iri ? byIri.get(c.parent_iri) : null
    if (parent) parent.children.push(node)
    else roots.push(node)
  }
  return roots
}

function ClassTreeNode({
  node,
  selectedId,
  onSelect,
}: {
  node: ClassNode
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const [open, setOpen] = useState(true)
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(node.cls.id)}
        className={[
          'flex w-full items-center gap-1 rounded px-2 py-1 text-left text-sm hover:bg-slate-50',
          selectedId === node.cls.id ? 'bg-sky-50' : '',
        ].join(' ')}
      >
        {node.children.length > 0 ? (
          <ChevronRight
            size={12}
            className={[
              'shrink-0 cursor-pointer text-slate-400 transition',
              open ? 'rotate-90' : '',
            ].join(' ')}
            onClick={(e) => {
              e.stopPropagation()
              setOpen((o) => !o)
            }}
          />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className="truncate font-medium text-slate-800">
          {node.cls.name}
        </span>
      </button>
      {open && node.children.length > 0 ? (
        <ul className="ml-4 mt-1 space-y-1 border-l border-slate-100 pl-2">
          {node.children.map((c) => (
            <ClassTreeNode
              key={c.cls.id}
              node={c}
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      ) : null}
    </li>
  )
}

function ClassDetailForm({
  ontologyId,
  cls,
  onSaved,
  onDelete,
  onError,
}: {
  ontologyId: string
  cls: OntologyClass
  onSaved: () => Promise<void> | void
  onDelete: () => Promise<void> | void
  onError: (msg: string | null) => void
}) {
  const [name, setName] = useState(cls.name)
  const [localName, setLocalName] = useState(cls.local_name ?? '')
  const [description, setDescription] = useState(cls.description ?? '')
  const [definition, setDefinition] = useState(cls.definition ?? '')
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  return (
    <div className="space-y-3 p-3">
      <Field label="IRI">
        <div className="font-mono text-xs text-slate-700">{cls.iri}</div>
      </Field>
      <Field label="类型">
        <span
          className={[
            'rounded px-1.5 py-0.5 text-[10px] uppercase',
            CLASS_TYPE_BADGE[cls.class_type] ?? 'bg-slate-100 text-slate-700',
          ].join(' ')}
        >
          {cls.class_type.toLowerCase()}
        </span>
      </Field>
      <Field label="名称">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <Field label="Local Name">
        <input
          value={localName}
          onChange={(e) => setLocalName(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <Field label="描述">
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <Field label="定义">
        <textarea
          value={definition}
          onChange={(e) => setDefinition(e.target.value)}
          rows={2}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <div className="flex gap-2 pt-2">
        <button
          type="button"
          disabled={saving || cls.is_locked}
          onClick={async () => {
            setSaving(true)
            try {
              await api.updateOntologyClass(ontologyId, cls.id, {
                name,
                local_name: localName,
                description,
                definition,
              })
              await onSaved()
            } catch (e) {
              onErr(e, onError)
            } finally {
              setSaving(false)
            }
          }}
          className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
        >
          {saving ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <PencilLine size={12} />
          )}
          保存
        </button>
        <button
          type="button"
          disabled={deleting || cls.is_locked}
          onClick={async () => {
            if (!window.confirm(`删除类 "${cls.name}"?`)) return
            setDeleting(true)
            try {
              await onDelete()
            } finally {
              setDeleting(false)
            }
          }}
          className="flex items-center gap-1.5 rounded-md border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50 disabled:opacity-50"
        >
          {deleting ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Trash2 size={12} />
          )}
          删除
        </button>
      </div>
    </div>
  )
}

function PropertyDetailForm({
  ontologyId,
  property,
  onSaved,
  onError,
}: {
  ontologyId: string
  property: OntologyProperty
  onSaved: () => Promise<void> | void
  onError: (msg: string | null) => void
}) {
  const [name, setName] = useState(property.name)
  const [localName, setLocalName] = useState(property.local_name ?? '')
  const [description, setDescription] = useState(property.description ?? '')
  const [unit, setUnit] = useState(property.unit ?? '')
  const [saving, setSaving] = useState(false)
  return (
    <div className="space-y-3 p-3">
      <Field label="IRI">
        <div className="font-mono text-xs text-slate-700">{property.iri}</div>
      </Field>
      <Field label="类型">
        <span
          className={[
            'rounded px-1.5 py-0.5 text-[10px] uppercase',
            PROPERTY_TYPE_BADGE[property.property_type] ??
              'bg-slate-100 text-slate-700',
          ].join(' ')}
        >
          {property.property_type.toLowerCase()}
        </span>
      </Field>
      <Field label="Domain">
        <div className="font-mono text-xs text-slate-700">
          {property.domain_iri ?? '—'}
        </div>
      </Field>
      <Field label="Range">
        <div className="font-mono text-xs text-slate-700">
          {property.range_class_iri ?? property.range_type ?? '—'}
        </div>
      </Field>
      <Field label="名称">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <Field label="Local Name">
        <input
          value={localName}
          onChange={(e) => setLocalName(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <Field label="单位">
        <input
          value={unit}
          onChange={(e) => setUnit(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <Field label="描述">
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
        />
      </Field>
      <div className="pt-2">
        <button
          type="button"
          disabled={saving || property.is_locked}
          onClick={async () => {
            setSaving(true)
            try {
              await api.updateOntologyProperty(ontologyId, property.id, {
                name,
                local_name: localName,
                description,
                unit,
              })
              await onSaved()
            } catch (e) {
              onErr(e, onError)
            } finally {
              setSaving(false)
            }
          }}
          className="flex items-center gap-1.5 rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
        >
          {saving ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <PencilLine size={12} />
          )}
          保存
        </button>
      </div>
    </div>
  )
}

function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div className="mt-1">{children}</div>
    </div>
  )
}

// ===== Versions Tab =====

function VersionsTab({
  ontologyId,
  onError,
}: {
  ontologyId: string
  onError: (msg: string | null) => void
}) {
  const [versions, setVersions] = useState<OntologyVersion[]>([])
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let alive = true
    setLoading(true)
    api
      .listOntologyVersions(ontologyId)
      .then((v) => {
        if (alive) setVersions(v)
      })
      .catch((e) => {
        if (alive) onErr(e, onError)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [ontologyId, onError])
  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载版本…
      </div>
    )
  }
  if (versions.length === 0) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        没有版本。
      </div>
    )
  }
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2 font-medium">Version</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Classes</th>
            <th className="px-3 py-2 font-medium">Properties</th>
            <th className="px-3 py-2 font-medium">Relations</th>
            <th className="px-3 py-2 font-medium">Baseline</th>
            <th className="px-3 py-2 font-medium">Published</th>
            <th className="px-3 py-2 font-medium">Created</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {versions.map((v) => (
            <tr key={v.id} className="hover:bg-slate-50">
              <td className="px-3 py-2 font-mono text-xs">{v.version}</td>
              <td className="px-3 py-2">
                <span
                  className={[
                    'rounded px-1.5 py-0.5 text-[10px] uppercase',
                    v.status === 'PUBLISHED'
                      ? 'bg-emerald-100 text-emerald-700'
                      : v.status === 'SUPERSEDED'
                        ? 'bg-slate-200 text-slate-500'
                        : 'bg-sky-100 text-sky-700',
                  ].join(' ')}
                >
                  {v.status.toLowerCase()}
                </span>
              </td>
              <td className="px-3 py-2 text-xs">{v.class_count}</td>
              <td className="px-3 py-2 text-xs">{v.property_count}</td>
              <td className="px-3 py-2 text-xs">{v.relation_count}</td>
              <td className="px-3 py-2 text-xs">
                {v.is_baseline ? '✓' : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {v.published_at
                  ? new Date(v.published_at).toLocaleString()
                  : '—'}
              </td>
              <td className="px-3 py-2 text-xs text-slate-600">
                {new Date(v.created_at).toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ===== Diff Tab =====

function DiffTab({
  ontologyId,
  onError,
}: {
  ontologyId: string
  onError: (msg: string | null) => void
}) {
  const [versions, setVersions] = useState<OntologyVersion[]>([])
  const [fromId, setFromId] = useState('')
  const [toId, setToId] = useState('')
  const [diff, setDiff] = useState<OntologyDiff | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    api
      .listOntologyVersions(ontologyId)
      .then((v) => {
        if (!alive) return
        setVersions(v)
        if (v.length >= 2) {
          setFromId(v[1].id)
          setToId(v[0].id)
        }
      })
      .catch((e) => {
        if (alive) onErr(e, onError)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [ontologyId, onError])

  const handleCompare = useCallback(async () => {
    if (!fromId || !toId) return
    setDiff(null)
    try {
      const d = await api.diffOntology(ontologyId, fromId, toId)
      setDiff(d)
    } catch (e) {
      onErr(e, onError)
    }
  }, [fromId, toId, ontologyId, onError])

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
        加载版本…
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-4">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">From</span>
          <select
            value={fromId}
            onChange={(e) => setFromId(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">—</option>
            {versions.map((v) => (
              <option key={v.id} value={v.id}>
                {v.version}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">To</span>
          <select
            value={toId}
            onChange={(e) => setToId(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">—</option>
            {versions.map((v) => (
              <option key={v.id} value={v.id}>
                {v.version}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={!fromId || !toId}
          onClick={() => void handleCompare()}
          className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50"
        >
          对比
        </button>
      </div>

      {diff ? <DiffView diff={diff} /> : null}
    </div>
  )
}

function DiffView({ diff }: { diff: OntologyDiff }) {
  const sections: Array<{
    title: string
    rows: Array<{ iri: string; name: string; change: string }>
  }> = [
    { title: 'Classes', rows: diff.class_diff },
    { title: 'Properties', rows: diff.property_diff },
    { title: 'Relations', rows: diff.relation_diff },
  ]

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-slate-200 bg-white p-3 text-sm">
        <strong>{diff.from_version}</strong> → <strong>{diff.to_version}</strong>
        <span className="ml-3 text-xs text-slate-500">
          added: {diff.summary.added ?? 0} · modified:{' '}
          {diff.summary.modified ?? 0} · removed: {diff.summary.removed ?? 0}
        </span>
      </div>
      {sections.map((s) => (
        <div
          key={s.title}
          className="overflow-hidden rounded-lg border border-slate-200 bg-white"
        >
          <header className="border-b border-slate-100 px-3 py-2 text-sm font-semibold text-slate-800">
            {s.title} ({s.rows.length})
          </header>
          {s.rows.length === 0 ? (
            <div className="p-3 text-xs text-slate-500">无变化。</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-1 font-medium">Change</th>
                  <th className="px-3 py-1 font-medium">IRI</th>
                  <th className="px-3 py-1 font-medium">Name</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {s.rows.map((row) => (
                  <tr key={row.iri} className="hover:bg-slate-50">
                    <td className="px-3 py-1">
                      <span
                        className={[
                          'rounded px-1.5 py-0.5 text-[10px] uppercase',
                          DIFF_BADGE[row.change] ?? 'bg-slate-100 text-slate-700',
                        ].join(' ')}
                      >
                        {row.change}
                      </span>
                    </td>
                    <td className="px-3 py-1 font-mono text-xs text-slate-700">
                      {row.iri}
                    </td>
                    <td className="px-3 py-1 text-sm text-slate-800">
                      {row.name}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
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
