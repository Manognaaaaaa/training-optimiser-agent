import { apiFetch } from './client'

export interface DayBreakdown {
  day: string
  sessions_completed: number
  attended: number
  no_shows: number
  new_bookings: number
  enrollments_cancelled: number
  sessions_cancelled: number
  sick_events: number
  drivers_activated: number
  scenario_events: string[]
}

export interface AdvanceSummary {
  from_time: string
  to_time: string
  days_advanced: number
  hit_year_end: boolean
  sessions_completed: number
  attended: number
  no_shows: number
  new_bookings: number
  enrollments_cancelled: number
  sessions_cancelled: number
  sick_events: number
  drivers_activated: number
  scenario_events_applied: string[]
  days: DayBreakdown[]
}

export interface SimTotals {
  drivers: number
  active_drivers: number
  sessions_scheduled: number
  sessions_completed: number
  sessions_cancelled: number
  enrollments_booked: number
  enrollments_attended: number
  enrollments_no_show: number
  enrollments_cancelled: number
}

export interface SimState {
  current_time: string
  year_start: string
  year_end: string
  percent_year_elapsed: number
  year_ended: boolean
  seed: number | null
  totals: SimTotals
}

export interface CourseProgress {
  course_id: number
  code: string
  name: string
  is_mandatory: boolean
  target: number
  attended: number
  percent_of_target: number
  booked_upcoming: number
  planned_capacity_remaining: number
}

const post = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) })

export interface AutoplayState {
  enabled: boolean
  step: 'day' | 'week'
  interval_seconds: number
  paused_reason: string | null
  steps_done: number
}

export const getAutoplay = () => apiFetch<AutoplayState>('/sim/autoplay')
export const setAutoplay = (body: { enabled: boolean; step?: 'day' | 'week'; interval_seconds?: number }) =>
  apiFetch<AutoplayState>('/sim/autoplay', post(body))
export const getSimState = () => apiFetch<SimState>('/sim/state')
export const getSimProgress = () => apiFetch<CourseProgress[]>('/sim/progress')
export const advanceSim = (days: number) => apiFetch<AdvanceSummary>('/sim/advance', post({ days }))
export const resetSim = (seed?: number) => apiFetch<SimState>('/sim/reset', post(seed === undefined ? {} : { seed }))
