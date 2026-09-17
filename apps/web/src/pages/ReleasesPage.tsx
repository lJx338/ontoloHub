import { PageHeader } from '../components/PageHeader'

export function ReleasesPage() {
  return (
    <PageHeader
      title="变更 / 发布"
      description="Change Request 工作流 + 发布线 + Preflight + Deployment。"
    >
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        占位 · 由{' '}
        <span className="font-mono">HIA-57 / HIA-61 (A10-A11)</span> 落地：CR 评审 +
        Release + Deployment API。
      </div>
    </PageHeader>
  )
}