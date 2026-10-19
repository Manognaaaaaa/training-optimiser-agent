import { useSyncExternalStore } from 'react'

/** Connection state of the live stream, shown in the top bar. */
export type LiveStatus = 'live' | 'reconnecting' | 'offline'

let status: LiveStatus = 'reconnecting'
const listeners = new Set<() => void>()

export function setLiveStatus(next: LiveStatus) {
  if (next === status) return
  status = next
  listeners.forEach((l) => l())
}

export const getLiveStatus = () => status

export function useLiveStatus(): LiveStatus {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => status,
  )
}
