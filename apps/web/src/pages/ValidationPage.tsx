import { PageHeader } from '../components/PageHeader'

export function ValidationPage() {
  return (
    <PageHeader
      title="验证"
      description="用 SHACL 对 Object / 数据快照做合规校验，给出违规清单。"
    >
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        占位 · 由 <span className="font-mono">HIA-58 (A9)</span> 落地：SHACL
        Verification Engine + 报告。
      </div>
    </PageHeader>
  )
}