import { PageHeader } from '../components/PageHeader'

export function ProjectsPage() {
  return (
    <PageHeader
      title="项目"
      description="业务项目的容器；一切本体 / 证据 / 候选都挂在项目下。"
    >
      <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
        占位 · 由 <span className="font-mono">HIA-51 (A2)</span> 落地：Project CRUD
        API + 列表 / 详情 / 编辑表单。
      </div>
    </PageHeader>
  )
}