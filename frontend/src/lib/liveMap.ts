/** Which cached queries go stale when the backend announces a change. Payloads carry only ids; the data is refetched. */
export type QueryKey = readonly unknown[]
export type LiveData = Record<string, unknown>

export const LIVE_EVENT_TYPES = [
  'sim.advanced', 'sim.autoplay_changed', 'sim.autoplay_paused', 'attendance.recorded', 'forecast.updated',
  'alert.created', 'alert.updated', 'planner_run.progress', 'planner_run.completed', 'plan.decided',
  'sessions.changed', 'master_data.changed', 'calendar.sync_updated', 'heartbeat', 'resync',
] as const

const TRACKING: QueryKey[] = [['tracking'], ['sim'], ['calendar'], ['sessions'], ['session'], ['enrollments']]
const ALERTS: QueryKey[] = [['alerts'], ['planner', 'runs']]

const MASTER: Record<string, QueryKey[]> = {
  drivers: [['drivers'], ['driver'], ['driver-options'], ['unavailability']],
  trainers: [['trainers'], ['all-trainers']],
  courses: [['courses'], ['all-courses'], ['tracking']],
}

/**
 * The query-key prefixes to invalidate for one event, 'all' for everything, or [] when the event is handled
 * another way (progress is written straight into the cache, heartbeats change nothing).
 */
export function keysFor(type: string, data: LiveData): QueryKey[] | 'all' {
  switch (type) {
    case 'resync':
      return 'all'
    case 'sim.advanced':
    case 'attendance.recorded':
    case 'forecast.updated':
      return TRACKING
    case 'sim.autoplay_changed':
    case 'sim.autoplay_paused':
      return [['sim', 'autoplay']]
    case 'alert.created':
    case 'alert.updated':
      return [...ALERTS, ...(typeof data.alert_id === 'number' ? [['alert', data.alert_id] as QueryKey] : [['alert'] as QueryKey])]
    case 'planner_run.completed':
      return [['alert'], ['alerts'], ['planner']]
    case 'plan.decided':
      return [['alert'], ['alerts'], ['planner', 'runs']]
    case 'sessions.changed':
      return [['calendar'], ['sessions'], ['session'], ['enrollments'], ['tracking'], ['sim'], ['calendar-sync']]
    case 'master_data.changed':
      return MASTER[String(data.entity)] ?? [['drivers'], ['trainers'], ['courses']]
    case 'calendar.sync_updated':
      return [['calendar'], ['calendar-sync'], ['alert'], ['sessions'], ['session']]
    default:
      return []
  }
}

/** What to tell the user, if anything. Shown immediately (not debounced). */
export function toastFor(type: string, data: LiveData): { level: 'success' | 'warning' | 'error' | 'info'; text: string; id: string } | null {
  const course = String(data.course_name ?? data.course_code ?? (data.course_id ? `course ${data.course_id}` : 'a course'))
  if (type === 'alert.created' && data.risk_level === 'high') {
    return { level: 'warning', text: `New high-risk alert: ${course}`, id: `alert-created-${data.alert_id}` }
  }
  if (type === 'alert.updated' && data.status === 'plans_ready') {
    return { level: 'info', text: `Plans are ready for ${course}`, id: `plans-ready-${data.alert_id}` }
  }
  if (type === 'plan.decided' && data.decision === 'approve') {
    return { level: 'success', text: `Plan ${data.plan_id} approved`, id: `plan-approved-${data.plan_id}` }
  }
  if (type === 'calendar.sync_updated' && data.status === 'failed') {
    return { level: 'error', text: `Calendar sync failed for session ${data.session_id}`, id: `sync-failed-${data.session_id}` }
  }
  if (type === 'sim.autoplay_paused') {
    return { level: 'info', text: `Auto-play paused: ${PAUSE_REASONS[String(data.reason)] ?? String(data.reason)}`, id: 'autoplay-paused' }
  }
  return null
}

export const PAUSE_REASONS: Record<string, string> = {
  planner_running: 'the planner is running',
  year_end: 'the simulated year has ended',
}
