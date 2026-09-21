import { createBrowserRouter, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { ProjectLayout } from './components/ProjectLayout'
import { DashboardPage } from './pages/DashboardPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { ProjectOverviewPage } from './pages/ProjectOverviewPage'
import { EvidenceInboxPage } from './pages/EvidenceInboxPage'
import { ReleasesPage } from './pages/ReleasesPage'
import { VerificationPage } from './pages/VerificationPage'
import { OntologyEditorPage } from './pages/OntologyEditorPage'

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
            element: <EvidenceInboxPage />,
          },
          {
            path: 'ontology',
            element: <OntologyEditorPage />,
          },
          {
            path: 'verify',
            element: <VerificationPage />,
          },
          {
            path: 'changes',
            element: <ReleasesPage />,
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
