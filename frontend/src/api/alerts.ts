import { apiFetch, json, qs } from './client'
import type { PlannerRun } from './planner'
import type { Reason, ShortfallType } from './tracking'

export interface AlertDetails {
  p_hit?: number
  low?: number
  high?: number
  projected?: number
  reasons?: Reason[]
  last_change?: 'opened' | 'escalated' | 'deescalated' | 'auto_resolved'
  last_change_at?: string
  previous_risk?: string | null
}

export type AlertStatus =
  | 'open' | 'generating' | 'plans_ready' | 'no_valid_plans' | 'approved' | 'rejected' | 'dismissed' | 'resolved' | 'expired' | 'superseded'

/** Alerts a course can have at most one of: the weekly check updates them in place. */
export const ACTIVE_STATUSES = 'open,generating,plans_ready,no_valid_plans'

export interface Alert {
  id: number
  course_id: number
  course_code: string
  course_name: string
  created_at: string
  updated_at: string | null
  risk_level: string
  shortfall_type: ShortfallType | null
  projected_completions: number
  target_completions: number
  gap: number
  message: string | null
  status: AlertStatus
  details: AlertDetails
}

export type PlanStatus = 'invalid' | 'valid' | 'approved' | 'rejected' | 'superseded' | 'stale' | 'expired'
export type ComponentName = 'closure' | 'confidence' | 'fairness' | 'disruption' | 'resource'

export interface ScoreBreakdown {
  components: Record<ComponentName, number>
  weights: Record<ComponentName, number>
  weighted: Record<ComponentName, number>
  p_hit_before: number
  p_hit_after: number
  expected_gain: number
  shortfall_gap: number
  drivers_moved: number
  sessions_added: number
  trainer_hours_added: number
  fairness_flag: boolean
  fairness_groups: Record<string, { burden: Record<string, number>; pool: Record<string, number> }>
}

export type Change =
  | { type: 'add_session'; temp_ref: string; when: string | null; trainer_name: string | null; location: string; capacity: number }
  | { type: 'enroll_driver'; driver_id: number; driver_name: string | null; shift: string | null; session_id: number | string; when: string | null }
  | {
      type: 'move_driver'; driver_id: number; driver_name: string | null; shift: string | null
      from_session_id: number; from_when: string | null; to_session_id: number | string; to_when: string | null
    }

export interface Violation {
  code: string
  category: string
  message: string
  action_index: number | null
}

export interface PlanView {
  id: number
  rank: number | null
  status: PlanStatus
  outcome: string
  source: 'llm' | 'llm_pruned' | 'greedy_baseline'
  parent_plan_id: number | null
  score_total: number | null
  score_breakdown: ScoreBreakdown | Record<string, never>
  fairness_flag: boolean
  rationale: string | null
  tradeoffs: string | null
  agent_rationale: string | null
  violations: Violation[]
  changes?: Change[]
}

export interface AlertMessageView {
  id: number
  model: string
  grounding_ok: boolean
  grounding_issues: { field: string; token: string; reason: string }[]
  used_fallback: boolean
  attempts: number
  message: {
    headline?: string
    summary?: string
    why_at_risk?: string[]
    caveats?: string[]
    recommended_plan_id?: number
  }
}

export interface DecisionView {
  id: number
  decision: 'approve' | 'reject_plan' | 'reject_alert' | 'dismiss'
  plan_id: number | null
  actor_name: string | null
  reason_code: string | null
  reason_text: string | null
  sim_time: string
}

export interface AlertDetail {
  alert: Alert
  forecast: { target: number; projected: number; attended: number | null; low: number | null; high: number | null; p_hit: number | null }
  message: AlertMessageView | null
  top_k: number
  presented: PlanView[]
  other_valid: PlanView[]
  invalid: PlanView[]
  run: PlannerRun | null
  decisions: DecisionView[]
}

export interface ApproveResult {
  plan_id: number
  alert_id: number
  status: string
  already_approved: boolean
  outbox: { id: number; change_type: string }[]
}

export const REJECT_REASONS: Record<string, string> = {
  not_feasible: 'Not feasible',
  unfair: 'Unfair to some drivers',
  too_disruptive: 'Too disruptive',
  trainer_unavailable: 'Trainer unavailable',
  other: 'Something else',
}

export const listAlerts = (params: { status?: string; risk_level?: string; course_id?: number; since?: string } = {}) =>
  apiFetch<Alert[]>(`/alerts${qs(params)}`)
export const getAlertCounts = () => apiFetch<{ by_status: Record<string, number>; plans_ready: number; total: number }>('/alerts/counts')
export const getAlert = (id: number) => apiFetch<AlertDetail>(`/alerts/${id}`)
export const generatePlans = (id: number, n: number) => apiFetch<PlannerRun>(`/alerts/${id}/generate`, json('POST', { n }))
export const regeneratePlans = (id: number, n: number) => apiFetch<PlannerRun>(`/alerts/${id}/regenerate`, json('POST', { n }))
export const approvePlan = (planId: number, actor: number | null) =>
  apiFetch<ApproveResult>(`/plans/${planId}/approve`, json('POST', { actor_trainer_id: actor }))
export const rejectPlan = (planId: number, body: { actor_trainer_id: number | null; reason_code: string; reason_text: string }) =>
  apiFetch<unknown>(`/plans/${planId}/reject`, json('POST', body))
export const rejectAlert = (id: number, body: { actor_trainer_id: number | null; reason_code: string; reason_text: string }) =>
  apiFetch<unknown>(`/alerts/${id}/reject`, json('POST', body))
export const dismissAlert = (id: number, actor: number | null, reason_text = '') =>
  apiFetch<unknown>(`/alerts/${id}/dismiss`, json('POST', { actor_trainer_id: actor, reason_text }))
