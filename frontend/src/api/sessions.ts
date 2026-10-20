import { apiFetch, json, qs, type ListParams, type Page } from './client'

export type FillBand = 'low' | 'medium' | 'high' | 'cancelled'

export interface Session {
  id: number
  course_id: number
  course_code: string
  course_name: string
  trainer_id: number
  trainer_name: string
  start_time: string
  end_time: string
  location: string
  capacity: number
  status: 'scheduled' | 'completed' | 'cancelled'
  source: string
  booked: number
  attended: number
  no_show: number
  fill_rate: number | null
  fill_band: FillBand
  is_editable: boolean
}

export interface SessionCreateInput {
  course_id: number
  trainer_id: number
  start_time: string
  location?: string
  capacity?: number
}

export interface SessionUpdateInput {
  trainer_id?: number
  start_time?: string
  location?: string
  capacity?: number
}

export interface Enrollment {
  id: number
  session_id: number
  driver_id: number
  employee_code: string
  name: string
  shift: string
  nationality: string
  status: string
}

export interface EligibleDriver {
  id: number
  employee_code: string
  name: string
  shift: string
  nationality: string
  depot: string
}

export interface CalendarEvent {
  id: number
  course_id: number
  course_code: string
  course_name: string
  trainer_id: number
  trainer_name: string
  start: string
  end: string
  location: string
  capacity: number
  status: Session['status']
  source: string
  booked: number
  attended: number
  fill_rate: number | null
  fill_band: FillBand
}

export interface CalendarResponse {
  sim_now: string
  events: CalendarEvent[]
}

export interface SessionFilters extends ListParams {
  course_id?: string
  trainer_id?: string
  status?: string
  start_from?: string
  start_to?: string
}

export const listSessions = (f: SessionFilters) => apiFetch<Page<Session>>(`/sessions${qs({ ...f })}`)
export const getSession = (id: number) => apiFetch<Session>(`/sessions/${id}`)
export const createSession = (body: SessionCreateInput) => apiFetch<Session>('/sessions', json('POST', body))
export const updateSession = (id: number, body: SessionUpdateInput) =>
  apiFetch<Session>(`/sessions/${id}`, json('PATCH', body))
export const cancelSession = (id: number) => apiFetch<Session>(`/sessions/${id}/cancel`, json('POST'))

export const listEnrollments = (id: number) => apiFetch<Enrollment[]>(`/sessions/${id}/enrollments`)
export const addEnrollment = (id: number, driverId: number) =>
  apiFetch<Enrollment>(`/sessions/${id}/enrollments`, json('POST', { driver_id: driverId }))
export const removeEnrollment = (sessionId: number, enrollmentId: number) =>
  apiFetch<Enrollment>(`/sessions/${sessionId}/enrollments/${enrollmentId}`, json('DELETE'))
export const listEligibleDrivers = (id: number, q: string) =>
  apiFetch<EligibleDriver[]>(`/sessions/${id}/eligible-drivers${qs({ q })}`)

export interface CalendarParams {
  start: string
  end: string
  course_id?: string
  trainer_id?: string
  include_cancelled?: boolean
}
export const getCalendar = (p: CalendarParams) => apiFetch<CalendarResponse>(`/calendar${qs({ ...p })}`)
