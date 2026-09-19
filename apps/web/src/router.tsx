import { createBrowserRouter, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { ProjectLayout, WorkbenchPlaceholder } from './components/ProjectLayout'
import { DashboardPage } from './pages/DashboardPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ProjectOverviewPage } from './pages/ProjectOverviewPage'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'projects', element: <ProjectsPage /> },

      // 项目工作台（A13 / HIA-63）：顶栏 + Tab + Outlet
      {
        path: 'projects/:projectId',
        element: <ProjectLayout />,
        children: [
          { index: true, element: <Navigate to="overview" replace /> },
          { path: 'overview', element: <ProjectOverviewPage /> },
          {
            path: 'evidence',
            element: (
              <WorkbenchPlaceholder
                title="证据收件箱"
                description="CSV / XLSX / DB 抽样 → 字段剖析 → 候选映射。"
                hia="HIA-60 (A15)"
              />
            ),
          },
          {
            path: 'ontology',
            element: (
              <WorkbenchPlaceholder
                title="本体编辑器"
                description="类 / 属性 / 关系 / 约束的可视化建模与版本管理。"
                hia="HIA-62 (A14)"
              />
            ),
          },
          {
            path: 'verify',
            element: (
              <WorkbenchPlaceholder
                title="验证"
                description="用 SHACL 对 Object / 数据快照做合规校验，给出违规清单。"
                hia="HIA-68 (A16)"
              />
            ),
          },
          {
            path: 'changes',
            element: (
              <WorkbenchPlaceholder
                title="变更 / 发布"
                description="Change Request 工作流 + 发布线 + Preflight + Deployment。"
                hia="HIA-65 (A17)"
              />
            ),
          },
        ],
      },

      // 老顶层路由保留作兼容入口（重定向到工作台首项）
      {
        path: 'evidence',
        element: <Navigate to="/projects" replace />,
      },
      {
        path: 'ontologies',
        element: <Navigate to="/projects" replace />,
      },
      {
        path: 'validation',
        element: <Navigate to="/projects" replace />,
      },
      {
        path: 'releases',
        element: <Navigate to="/projects" replace />,
      },

      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
