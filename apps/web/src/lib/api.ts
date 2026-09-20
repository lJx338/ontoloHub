// 共享 API client
// 通过 Vite dev proxy 把 `/api/*` 和 `/health` 转发到 http://localhost:8000
//
// 注意：后端实际路由是顶层分组的（`/projects`、`/ontologies`、`/sources`、`/validation` …），
// 由 ID 字段关联资源，不通过 `/projects/{id}/...` 嵌套。

const BASE = '/api'

export class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `API ${status}`)
    this.status = status
    this.body = body
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { params?: Record<string, string | number | boolean | undefined | null> },
): Promise<T> {
  const url = new URL(BASE + path, window.location.origin)
  if (init?.params) {
    for (const [k, v] of Object.entries(init.params)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v))
    }
  }
  const res = await fetch(url.toString().replace(window.location.origin, ''), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    let body: unknown = null
    try {
      body = await res.json()
    } catch {
      // ignore non-JSON body
    }
    throw new ApiError(res.status, body)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  health: () =>
    request<{ status: string; version: string }>('/health'),

  // Projects (/projects)
  listProjects: () => request<import('./types').Project[]>('/projects'),
  getProject: (id: string) => request<import('./types').Project>(`/projects/${id}`),
  createProject: (input: { name: string; description?: string }) =>
    request<import('./types').Project>('/projects', {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  patchProject: (
    id: string,
    input: Partial<{ name: string; description: string; status: string }>,
  ) =>
    request<import('./types').Project>(`/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(input),
    }),
  deleteProject: (id: string) =>
    request<void>(`/projects/${id}`, { method: 'DELETE' }),

  // Evidence (/projects/{id}/evidences — 复数)
  listProjectEvidences: (projectId: string) =>
    request<import('./types').Evidence[]>(`/projects/${projectId}/evidences`),
  getEvidence: (projectId: string, evidenceId: string) =>
    request<import('./types').Evidence>(`/projects/${projectId}/evidences/${evidenceId}`),
  uploadEvidence: (projectId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return fetch(`${BASE}/projects/${projectId}/evidences/upload`, {
      method: 'POST',
      body: form,
    }).then(async (res) => {
      if (!res.ok) {
        let body: unknown = null
        try {
          body = await res.json()
        } catch {
          /* ignore */
        }
        throw new ApiError(res.status, body)
      }
      return (await res.json()) as import('./types').EvidenceUploadResult
    })
  },

  // Candidates (/candidates)
  generateCandidatesFromEvidence: (projectId: string) =>
    request<import('./types').CandidateGenerationResponse>(
      `/candidates/from-evidence/project/${projectId}`,
      { method: 'POST', body: JSON.stringify({}) },
    ),

  // Proposals (/proposals)
  listProposals: (
    projectId: string,
    filter?: { status?: import('./types').ProposalStatus; proposal_type?: import('./types').ProposalType },
  ) =>
    request<import('./types').Proposal[]>('/proposals', {
      params: {
        project_id: projectId,
        status: filter?.status,
        proposal_type: filter?.proposal_type,
      },
    }),
  decideProposal: (
    proposalId: string,
    input: {
      decision: 'accept' | 'reject' | 'merge'
      notes?: string
      target_class_id?: string
      target_property_id?: string
    },
  ) =>
    request<unknown>(`/proposals/${proposalId}/decisions`, {
      method: 'POST',
      body: JSON.stringify(input),
    }),
  batchReviewProposals: (
    proposalIds: string[],
    decision: 'accept' | 'reject',
  ) =>
    request<import('./types').BatchReviewResult>('/proposals/batch-review', {
      method: 'POST',
      body: JSON.stringify(proposalIds),
      params: { decision },
    }),

  // Ontology (/ontologies?project_id=…)
  listOntologies: (projectId?: string) =>
    request<import('./types').Ontology[]>('/ontologies', {
      params: { project_id: projectId },
    }),

  // Change Requests (/change-requests) — HIA-65
  listProjectChangeRequests: (projectId: string) =>
    request<import('./types').ChangeRequest[]>('/change-requests', {
      params: { project_id: projectId },
    }),
  getChangeRequest: (id: string) =>
    request<import('./types').ChangeRequest>(`/change-requests/${id}`),
  getChangeRequestDiff: (id: string) =>
    request<import('./types').ChangeRequestDiff>(
      `/change-requests/${id}/diff`,
    ),
  submitChangeRequest: (id: string) =>
    request<import('./types').ChangeRequest>(`/change-requests/${id}/submit`, {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  approveChangeRequest: (id: string, notes?: string) =>
    request<import('./types').ChangeRequest>(`/change-requests/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ notes }),
    }),
  rejectChangeRequest: (id: string, notes?: string) =>
    request<import('./types').ChangeRequest>(`/change-requests/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ notes }),
    }),
  mergeChangeRequest: (id: string) =>
    request<import('./types').ChangeRequest>(`/change-requests/${id}/merge`, {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  listChangeRequestReviewers: (id: string) =>
    request<import('./types').Reviewer[]>(`/change-requests/${id}/reviewers`),

  // Releases (/releases) — HIA-65
  listProjectReleases: (projectId: string) =>
    request<import('./types').Release[]>(
      `/projects/${projectId}/releases`,
    ),
  getRelease: (id: string) =>
    request<import('./types').Release>(`/releases/${id}`),

  // Deployments — HIA-65
  listProjectDeployments: (projectId: string) =>
    request<import('./types').Deployment[]>(
      `/projects/${projectId}/deployments`,
    ),

  // Audit — HIA-65 / HIA-80
  listProjectAudit: (
    projectId: string,
    limit?: number,
  ) =>
    request<import('./types').AuditEvent[]>(
      `/projects/${projectId}/audit`,
      { params: { limit } },
    ),

  // Verification — HIA-68
  listProjectValidationRuns: (
    projectId: string,
    limit?: number,
  ) =>
    request<import('./types').ValidationRun[]>('/validation/runs', {
      params: { project_id: projectId, limit },
    }),
  getValidationRun: (runId: string) =>
    request<import('./types').ValidationRunDetail>(
      `/validation/runs/${runId}`,
    ),
  createValidationRun: (
    projectId: string,
    input: import('./types').ValidationRunCreateInput,
  ) =>
    request<import('./types').ValidationRun>('/validation/runs', {
      method: 'POST',
      params: { project_id: projectId },
      body: JSON.stringify(input),
    }),
  executeValidationRun: (runId: string) =>
    request<import('./types').ValidationRunDetail>(
      `/validation/runs/${runId}/execute`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  saveValidationRunAsQuery: (
    runId: string,
    input: { name: string; description?: string; use_case?: string },
  ) =>
    request<import('./types').SavedQuery>(
      `/validation/runs/${runId}/save-as-query`,
      { method: 'POST', body: JSON.stringify(input) },
    ),
}
