/** Remembers the last few clock advances, so the Simulation page can show a short activity feed. Kept in this browser only. */
import { useSyncExternalStore } from 'react'
import type { AdvanceSummary } from '../api/sim'

export interface SimLogEntry {
  from: string
  to: string
  days: number
  attended: number
  noShows: number
  newBookings: number
  sessionsCompleted: number
}

const KEY = 'sim-advance-log'
const MAX_ENTRIES = 8
const listeners = new Set<() => void>()

function read(): SimLogEntry[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? '[]') as SimLogEntry[]
  } catch {
    return []
  }
}

let cache = read()

function write(entries: SimLogEntry[]) {
  cache = entries
  try {
    localStorage.setItem(KEY, JSON.stringify(entries))
  } catch {
    /* storage can be blocked; the feed then lasts until the page is closed */
  }
  listeners.forEach((l) => l())
}

export function logAdvance(s: AdvanceSummary) {
  write([
    { from: s.from_time, to: s.to_time, days: s.days_advanced, attended: s.attended, noShows: s.no_shows, newBookings: s.new_bookings, sessionsCompleted: s.sessions_completed },
    ...cache,
  ].slice(0, MAX_ENTRIES))
}

export const clearAdvanceLog = () => write([])

export function useAdvanceLog(): SimLogEntry[] {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    () => cache,
  )
}
