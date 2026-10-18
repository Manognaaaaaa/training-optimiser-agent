import { apiFetch, qs } from './client'

export type RunStatus = 'queued' | 'running' | 'done' | 'failed'
export type Outcome = 'valid' | 'invalid' | 'schema_invalid' | 'no_plan' | 'step_limit'

export interface PlannerRun {
  id: number
  alert_id: number
  course_id: number
  status: RunStatus
  n_requested: number
  samples_done: number
  n_valid: number
  n_invalid: number
  n_schema_invalid: number
  n_no_plan: number
  n_step_limit: number
  n_duplicates: number
  n_llm_errors: number
  model: string
  prompt_version: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number
  total_tokens: number
  error: string | null
}

export interface PlanViolation {
  code: string
  category: string
  message: string
  action_index: number | null
}

export interface PlannerPlan {
  id: number
  run_id: number
  sample_index: number
  strategy: string
  temperature: number
  outcome: Outcome
  is_valid: boolean
  is_duplicate: boolean
  rationale: string | null
  action_labels: string[]
  violations: PlanViolation[]
  fabricated_ids: { type: string; id: number }[]
  checker_caught: boolean | null
  n_steps: number
  n_validate_calls: number
  total_tokens: number
  latency_ms: number
}

export interface PlannerRunDetail extends PlannerRun {
  plans: PlannerPlan[]
}

export interface PlannerMetrics {
  plans_total: number
  outcomes: Record<string, number>
  valid_pct: number
  schema_invalid_pct: number
  hallucination_pct: number
  fabricated_ids_total: number
  fabricated_ids_caught: number
  checker_catch_rate: number | null
  avg_steps: number
  avg_tokens: number
  avg_latency_ms: number
  runs_total: number
}

export const startPlannerRun = (alertId: number, n: number) =>
  apiFetch<PlannerRun>('/planner/runs', { method: 'POST', body: JSON.stringify({ alert_id: alertId, n }) })
export const listPlannerRuns = (params: { alert_id?: number } = {}) => apiFetch<PlannerRun[]>(`/planner/runs${qs(params)}`)
export const getPlannerRun = (id: number) => apiFetch<PlannerRunDetail>(`/planner/runs/${id}`)
export const getPlannerMetrics = () => apiFetch<PlannerMetrics>('/planner/metrics')

export const isRunActive = (run: { status: RunStatus } | undefined) => run?.status === 'queued' || run?.status === 'running'
