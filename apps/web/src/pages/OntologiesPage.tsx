import { PageHeader } from '../components/PageHeader'

export function OntologiesPage() {
  return (
    <PageHeader
      title="本体"
      description="类 / 属性 / 关系 / 约束的可视化建模与版本管理。"
    >
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        占位 · 由 <span className="font-mono">HIA-56 (A7)</span> 落地：Ontology +
        OntologyVersion CRUD API + 编辑器。
      </div>
    </PageHeader>
  )
}