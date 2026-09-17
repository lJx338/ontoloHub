import { PageHeader } from '../components/PageHeader'

export function EvidenceInboxPage() {
  return (
    <PageHeader
      title="证据收件箱"
      description="CSV / XLSX / DB 抽样 → 字段剖析 → 候选映射。"
    >
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        占位 · 由{' '}
        <span className="font-mono">HIA-49 / HIA-53 / HIA-52 / HIA-55 (A3-A6)</span>{' '}
        落地：上传 + 剖析执行 + 候选生成 + 决策流。
      </div>
    </PageHeader>
  )
}