import { useQueryClient, type QueryKey } from '@tanstack/react-query'
import { useEffect } from 'react'
import { toast } from 'sonner'
import { BASE_URL } from '../api/client'
import type { PlannerRun } from '../api/planner'
import { LIVE_EVENT_TYPES, keysFor, toastFor, type LiveData } from '../lib/liveMap'
import { setLiveStatus } from '../live/liveStore'

const DEBOUNCE_MS = 300
const OFFLINE_AFTER_MS = 10_000
const FALLBACK_POLL_MS = 15_000

/**
 * One EventSource for the whole app. Each event invalidates the queries it affects (batched for 300 ms so auto-play
 * cannot cause a refetch storm). Planner progress is written straight into the cache.
 * If the stream is down for more than 10 s the status becomes "offline" and active queries are refreshed every 15 s.
 */
export function useLiveEvents() {
  const queryClient = useQueryClient()

  useEffect(() => {
    let source: EventSource | null = null
    let closed = false
    let wasDown = false
    let flushTimer: ReturnType<typeof setTimeout> | undefined
    let offlineTimer: ReturnType<typeof setTimeout> | undefined
    let pollTimer: ReturnType<typeof setInterval> | undefined
    const pending = new Map<string, QueryKey>()
    let everything = false

    const flush = () => {
      flushTimer = undefined
      if (everything) {
        void queryClient.invalidateQueries()
      } else {
        for (const key of pending.values()) void queryClient.invalidateQueries({ queryKey: key })
      }
      everything = false
      pending.clear()
    }
    const schedule = () => {
      if (flushTimer === undefined) flushTimer = setTimeout(flush, DEBOUNCE_MS)
    }

    const applyProgress = (data: LiveData) => {
      const runId = data.run_id
      const patch = (run: PlannerRun): PlannerRun =>
        run.id === runId ? { ...run, samples_done: Number(data.samples_done ?? run.samples_done), status: String(data.status ?? run.status) as PlannerRun['status'] } : run
      queryClient.setQueriesData<PlannerRun>({ queryKey: ['planner', 'run'] }, (old) => (old ? patch(old) : old))
      queryClient.setQueriesData<PlannerRun[]>({ queryKey: ['planner', 'runs'] }, (old) => (Array.isArray(old) ? old.map(patch) : old))
      queryClient.setQueriesData<{ run: PlannerRun | null }>({ queryKey: ['alert'] }, (old) =>
        old && typeof old === 'object' && 'run' in old && old.run ? { ...old, run: patch(old.run) } : old)
    }

    const handle = (type: string, raw: string) => {
      let data: LiveData = {}
      try {
        data = JSON.parse(raw || '{}') as LiveData
      } catch {
        /* a malformed payload still means "something changed" */
      }
      if (type === 'planner_run.progress') {
        applyProgress(data)
        return
      }
      const note = toastFor(type, data)
      if (note) toast[note.level](note.text, { id: note.id })
      const keys = keysFor(type, data)
      if (keys === 'all') everything = true
      else for (const k of keys) pending.set(JSON.stringify(k), k)
      if (keys === 'all' || keys.length > 0) schedule()
    }

    const markUp = () => {
      clearTimeout(offlineTimer)
      offlineTimer = undefined
      clearInterval(pollTimer)
      pollTimer = undefined
      setLiveStatus('live')
      if (wasDown) {
        wasDown = false
        everything = true // we may have missed events while away
        schedule()
      }
    }
    const markDown = () => {
      wasDown = true
      setLiveStatus('reconnecting')
      if (offlineTimer === undefined) {
        offlineTimer = setTimeout(() => {
          setLiveStatus('offline')
          pollTimer ??= setInterval(() => void queryClient.invalidateQueries({ type: 'active' }), FALLBACK_POLL_MS)
        }, OFFLINE_AFTER_MS)
      }
    }

    const connect = () => {
      if (closed) return
      source = new EventSource(`${BASE_URL}/live/stream`)
      source.onopen = markUp
      source.onerror = () => {
        markDown()
        // The browser retries by itself, but not after a hard failure (for example a 404): reconnect manually then.
        if (source && source.readyState === EventSource.CLOSED) {
          source.close()
          setTimeout(connect, 3000)
        }
      }
      for (const type of LIVE_EVENT_TYPES) {
        source.addEventListener(type, (e) => {
          if (type === 'heartbeat') {
            markUp()
            return
          }
          handle(type, (e as MessageEvent).data)
        })
      }
    }
    connect()

    return () => {
      closed = true
      source?.close()
      clearTimeout(flushTimer)
      clearTimeout(offlineTimer)
      clearInterval(pollTimer)
    }
  }, [queryClient])
}
