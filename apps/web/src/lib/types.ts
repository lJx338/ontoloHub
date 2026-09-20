// 共享领域类型 — 与后端响应模型对齐

export type ProjectStatus =
  | 'discovery'
  | 'modeling'
  | 'validation'
  | 'delivery'
  | 'support'
  | 'archived'

export interface Project {
  id: string
  name: string
  description?: string | null
  status: ProjectStatus
  customer_name?: string | null
  target_environment?: string | null
  owner_name?: string | null
  business_owner?: string | null
  business_owner_email?: string | null
  my_role?: string | null
  created_at: string
  updated_at: string
}

export type OntologyKind = 'PROJECT' | 'BASE' | 'INDUSTRY' | 'STANDARD'
export type OntologyStatus = 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'

export interface Ontology {
  id: string
  name: string
  namespace: string
  project_id?: string | null
  kind: OntologyKind
  status: OntologyStatus
  standard_name?: string | null
}

export type EvidenceType =
  | 'SOURCE_FIELD'
  | 'SOURCE_RECORD'
  | 'DOCUMENT'
  | 'DOMAIN_EXPERT'
  | 'STANDARD'
  | 'INFERRED'

export type EvidenceStrength = 'low' | 'medium' | 'high'

export interface Evidence {
  id: string
  evidence_type: EvidenceType
  location?: string | null
  field_name?: string | null
  record_id?: string | null
  content?: string | null
  source_identifier?: string | null
  source_url?: string | null
  ontology_class_iri?: string | null
  property_iri?: string | null
  is_confirmed: boolean
  strength: EvidenceStrength
  notes?: string | null
  created_by?: string | null
  created_at: string
}

export interface EvidenceUploadResult {
  evidence: Evidence
  file_stored_path?: string | null
  content_hash?: string | null
  file_size: number
}

export type ProposalType = 'CLASS' | 'PROPERTY' | 'RELATION' | 'CONSTRAINT' | 'MAPPING'
export type ProposalStatus = 'PENDING' | 'ACCEPTED' | 'REJECTED' | 'MERGED' | 'SUPERSEDED'
export type ConfidenceLevel = 'HIGH' | 'MEDIUM' | 'LOW' | 'UNCALIBRATED'

export interface Proposal {
  id: string
  title: string
  description?: string | null
  proposal_type: ProposalType
  status: ProposalStatus
  suggested_iri?: string | null
  confidence: ConfidenceLevel
  confidence_score?: number | null
  source?: string | null
  model_name?: string | null
  reviewed_by?: string | null
  reviewed_at?: string | null
  created_at: string
}

export interface CandidateProfileItem {
  field_name: string
  inferred_type: string
  confidence: number
  confidence_level: ConfidenceLevel | string
}

export interface ValueOverlapGroup {
  field_names: string[]
  min_jaccard: number
}

export interface CandidateGenerationResponse {
  proposals_created: number
  proposals_skipped: number
  field_profiles: CandidateProfileItem[]
  value_overlap_groups: ValueOverlapGroup[]
  primary_key_candidates: string[]
}

export interface BatchReviewResult {
  total: number
  accepted: number
  rejected: number
  skipped: number
}

// ===== Change Request / Release / Deployment (HIA-65) =====

export type ChangeRequestStatus =
  | 'DRAFT'
  | 'SUBMITTED'
  | 'CHANGES_REQUESTED'
  | 'APPROVED'
  | 'MERGED'
  | 'CLOSED'

export interface ChangeRequest {
  id: string
  project_id: string
  title: string
  description?: string | null
  status: ChangeRequestStatus
  baseline_version_id?: string | null
  baseline_version?: string | null
  target_version_id?: string | null
  target_version?: string | null
  changes: Record<string, unknown>
  changes_summary?: string | null
  impact_scope?: Record<string, unknown> | null
  required_approvers: number
  submitted_by?: string | null
  submitted_at?: string | null
  reviewed_by?: string | null
  reviewed_at?: string | null
  review_notes?: string | null
  approved_by?: string | null
  approved_at?: string | null
  merged_at?: string | null
  merged_by?: string | null
  closed_at?: string | null
  closed_by?: string | null
  close_reason?: string | null
  created_by?: string | null
  created_at: string
  updated_at: string
}

export type ReleaseStatus =
  | 'DRAFT'
  | 'PUBLISHED'
  | 'SUPERSEDED'
  | 'YANKED'
  | 'DEPRECATED'

export interface Release {
  id: string
  project_id: string
  version: string
  status: ReleaseStatus
  description?: string | null
  ontology_version_id?: string | null
  ontology_version?: string | null
  mapping_version_id?: string | null
  mapping_version?: string | null
  artifacts?: Record<string, unknown> | null
  checksum?: string | null
  artifact_size?: number | null
  validation_results?: Record<string, unknown> | null
  released_at?: string | null
  released_by?: string | null
  created_at: string
  updated_at: string
}

export type DeploymentStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'ROLLED_BACK'

export interface Deployment {
  id: string
  release_id: string
  project_id: string
  environment: string
  environment_type?: string | null
  status: DeploymentStatus
  configuration?: Record<string, unknown> | null
  started_at?: string | null
  completed_at?: string | null
  duration_ms?: number | null
  result?: Record<string, unknown> | null
  error_message?: string | null
  deployed_by?: string | null
  deployed_by_name?: string | null
  created_at: string
  updated_at: string
}

export interface ChangeRequestDiff {
  baseline_version?: string | null
  target_version?: string | null
  added_classes: Array<Record<string, unknown>>
  modified_classes: Array<Record<string, unknown>>
  removed_classes: Array<Record<string, unknown>>
  added_properties: Array<Record<string, unknown>>
  modified_properties: Array<Record<string, unknown>>
  removed_properties: Array<Record<string, unknown>>
}

export interface Reviewer {
  id: string
  change_request_id: string
  reviewer_id: string
  reviewer_name?: string | null
  status: string
  reviewed_at?: string | null
  comment?: string | null
  created_at: string
  updated_at: string
}

export interface CrComment {
  id: string
  change_request_id: string
  author_id?: string | null
  author_name?: string | null
  body: string
  parent_id?: string | null
  created_at: string
  updated_at: string
}

// ===== Audit (HIA-65 audit tab / HIA-80) =====

export type AuditEventType =
  | 'CREATE'
  | 'UPDATE'
  | 'DELETE'
  | 'READ'
  | 'LOGIN'
  | 'LOGOUT'

export interface AuditEvent {
  id: string
  event_type: AuditEventType
  actor_id?: string | null
  actor_name?: string | null
  actor_ip?: string | null
  project_id?: string | null
  target_type?: string | null
  target_id?: string | null
  target_label?: string | null
  before?: Record<string, unknown> | null
  after?: Record<string, unknown> | null
  prev_hash?: string | null
  entry_hash?: string | null
  created_at: string
  notes?: string | null
}

// ===== Verification / Validation (HIA-68) =====

export type ValidationStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'

export interface ValidationRun {
  id: string
  project_id: string
  validation_type: string
  status: ValidationStatus
  total_tests: number
  passed_tests: number
  failed_tests: number
  skipped_tests: number
  duration_ms?: number | null
  started_at?: string | null
  completed_at?: string | null
  created_at: string
}

export interface SHACLViolation {
  focus_node?: string
  path?: string
  message?: string
  severity?: string
  value?: unknown
  source_shape?: string
}

export interface ValidationRunDetail {
  id: string
  project_id: string
  name: string
  description?: string | null
  validation_type: string
  status: ValidationStatus
  total_checks: number
  passed_checks: number
  warning_checks: number
  failed_checks: number
  violations?: SHACLViolation[] | null
  report_summary?: Record<string, unknown> | null
  started_at?: string | null
  completed_at?: string | null
  duration_ms?: number | null
  triggered_by?: string | null
  created_by?: string | null
  created_at: string
}

export interface ValidationRunCreateInput {
  validation_type: string
  ontology_version_id?: string
  mapping_version_id?: string
  config?: Record<string, unknown>
}

export interface SavedQuery {
  id: string
  project_id: string
  name: string
  description?: string | null
  is_shared: boolean
  run_count: number
  last_run_at?: string | null
  created_by?: string | null
  created_at: string
}
