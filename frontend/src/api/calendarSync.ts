import { apiFetch, json, qs } from './client'

export type SyncStatus = 'none' | 'pending' | 'synced' | 'failed' | 'cancelled' | 'drift'

/** Google Calendar state of one session, added to session, calendar and plan payloads. */
export interface CalendarInfo {
  status: SyncStatus
  html_link: string | null
  last_synced_at: string | null
}

export interface OutboxRow {
  id: number
  session_id: number | null
  plan_id: number | null
  alert_id: number | null
  op: 'upsert' | 'cancel'
  source: string
  change_type: string
  status: 'pending' | 'in_progress' | 'done' | 'failed' | 'skipped'
  attempts: number
  next_attempt_at: string | null
  last_error: string | null
  created_at: string | null
  completed_at: string | null
}

export interface SyncSummary {
  provider: 'fake' | 'google'
  calendar_id: string
  enabled: boolean
  counts: Record<OutboxRow['status'], number>
  links: Record<string, number>
  last_success_at: string | null
  recent: OutboxRow[]
}

export interface ReconcileReport {
  from: string
  to: string
  remote_events: number
  in_sync: number
  missing_in_google: { session_id: number; event_id: string }[]
  orphaned_in_google: { event_id: string; session_id: number | null; summary: string | null }[]
  content_mismatch: { session_id: number; event_id: string }[]
}

export interface AutoplayState {
  enabled: boolean
  step: 'day' | 'week'
  interval_seconds: number
  paused_reason: string | null
  steps_done: number
}

export const getSyncSummary = () => apiFetch<SyncSummary>('/calendar/sync/status')
export const getFailedRows = () => apiFetch<OutboxRow[]>('/calendar/sync/failed')
export const retryRow = (id: number) => apiFetch<OutboxRow>(`/calendar/sync/retry/${id}`, json('POST'))
export const retryAllFailed = () => apiFetch<{ requeued: number }>('/calendar/sync/retry-all-failed', json('POST'))
export const backfill = (from_date: string, to_date: string) =>
  apiFetch<{ enqueued: number }>('/calendar/backfill', json('POST', { from_date, to_date }))
export const reconcile = (from: string, to: string) => apiFetch<ReconcileReport>(`/calendar/reconcile${qs({ from, to })}`)
export const resetCalendar = (confirm: string) => apiFetch<{ events_deleted: number; links_cleared: number }>('/calendar/reset', json('POST', { confirm }))

export const getAutoplay = () => apiFetch<AutoplayState>('/sim/autoplay')
export const setAutoplay = (body: { enabled: boolean; step?: 'day' | 'week'; interval_seconds?: number }) =>
  apiFetch<AutoplayState>('/sim/autoplay', json('POST', body))

export const SYNC_LABEL: Record<SyncStatus, string> = {
  none: 'Not in Google Calendar',
  pending: 'Waiting to sync',
  synced: 'In sync with Google Calendar',
  failed: 'Sync failed',
  cancelled: 'Removed from Google Calendar',
  drift: 'Edited in Google Calendar (will be overwritten)',
}
