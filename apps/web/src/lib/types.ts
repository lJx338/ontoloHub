/**
 * 后端 API 响应的 TypeScript 类型镜像。
 *
 * 注意：这些是「最薄」的描述，只覆盖 HIA-70 前端需要的字段；
 * 后端字段若有扩展不会破坏前端（多余字段被忽略）。
 */

export type FunctionLanguage = 'python' | 'javascript' | 'typescript'

export interface FunctionRecord {
  id: string
  project_id: string
  api_name: string
  display_name: string
  description: string | null
  language: FunctionLanguage
  source_code: string
  version: number
  parameters_schema: Record<string, unknown> | null
  return_schema: Record<string, unknown> | null
  config: Record<string, unknown> | null
  created_by: string | null
  created_at: string
  updated_at: string
}

export interface FunctionTestResponse {
  run_id: string
  function_id: string
  version: number
  output_data: unknown
  stdout: string | null
  stderr: string | null
  error: string | null
  duration_ms: number
  timed_out: boolean
}

export interface FunctionRunRecord {
  id: string
  project_id: string
  function_id: string
  input_data: Record<string, unknown> | null
  output_data: Record<string, unknown> | null
  error: string | null
  duration_ms: number | null
  triggered_by: string | null
  created_at: string
}

export type ActionKind = 'function' | 'webhook' | 'workflow'
export type ActionStatus = 'draft' | 'published' | 'deprecated'

export interface ActionTypeRecord {
  id: string
  project_id: string
  name: string
  description: string | null
  kind: ActionKind
  status: ActionStatus
  parameters_schema: Record<string, unknown> | null
  return_schema: Record<string, unknown> | null
  code: string | null
  runtime: string | null
  config: Record<string, unknown> | null
  version: number
  created_by: string | null
  created_at: string
  updated_at: string
}

export type ActionRunStatus = 'pending' | 'running' | 'success' | 'failed' | 'canceled'

export interface ActionRunRecord {
  id: string
  project_id: string
  action_type_id: string
  status: ActionRunStatus
  input_data: Record<string, unknown> | null
  output_data: Record<string, unknown> | null
  error: string | null
  started_at: string | null
  completed_at: string | null
  duration_ms: number | null
  triggered_by: string | null
  created_at: string
}

export interface ProjectRecord {
  id: string
  name: string
  description: string | null
  status: string
  customer_name: string | null
  target_environment: string | null
  owner_name: string | null
  business_owner: string | null
  business_owner_email: string | null
  my_role: string | null
  created_at: string
  updated_at: string
}
