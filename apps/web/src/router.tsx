import { createBrowserRouter, Navigate } from 'react-router-dom'
import { Layout } from './components/Layout'
import { DashboardPage } from './pages/DashboardPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { OntologiesPage } from './pages/OntologiesPage'
import { EvidenceInboxPage } from './pages/EvidenceInboxPage'
import { ValidationPage } from './pages/ValidationPage'
import { ReleasesPage } from './pages/ReleasesPage'
import { FunctionsPage } from './pages/FunctionsPage'
import { ActionsPage } from './pages/ActionsPage'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'projects', element: <ProjectsPage /> },
      { path: 'ontologies', element: <OntologiesPage /> },
      { path: 'evidence', element: <EvidenceInboxPage /> },
      { path: 'validation', element: <ValidationPage /> },
      { path: 'releases', element: <ReleasesPage /> },
      { path: 'functions', element: <FunctionsPage /> },
      { path: 'actions', element: <ActionsPage /> },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
