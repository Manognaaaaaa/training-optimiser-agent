import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useState } from 'react'
import { listAlerts, type Alert } from '../api/alerts'
import { isRunActive, listPlannerRuns } from '../api/planner'
import { RiskBadge } from '../components/Badge'
import {
  advanceSim,
  getSimProgress,
  getSimState,
  resetSim,
  type AdvanceSummary,
  type CourseProgress,
  type SimState,
} from '../api/sim'

const ADVANCE_BUTTONS = [
  { label: '+1 day', days: 1 },
  { label: '+1 week', days: 7 },
  { label: '+1 month', days: 30 },
]

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })
}

/** What changed in the last advance, read from the alerts' own change markers (last_change / last_change_at). */
function riskChanges(alerts: Alert[], since: string) {
  const out = { opened: [] as Alert[], escalated: [] as Alert[], deescalated: [] as Alert[], resolved: [] as Alert[] }
  for (const a of alerts) {
    const at = a.details.last_change_at
    if (!at || at <= since) continue
    if (a.details.last_change === 'opened') out.opened.push(a)
    else if (a.details.last_change === 'escalated') out.escalated.push(a)
    else if (a.details.last_change === 'deescalated') out.deescalated.push(a)
    else if (a.details.last_change === 'auto_resolved') out.resolved.push(a)
  }
  return out
}

const cell = 'px-3 py-2 text-sm'
const headCell = 'px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-500'

export default function Simulation() {
  const [state, setState] = useState<SimState | null>(null)
  const [progress, setProgress] = useState<CourseProgress[]>([])
  const [summary, setSummary] = useState<AdvanceSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const queryClient = useQueryClient()
  // While the planner works from a snapshot of "now", the backend refuses to move the clock (409).
  const plannerRuns = useQuery({ queryKey: ['planner', 'runs', 'all'], queryFn: () => listPlannerRuns(), refetchInterval: 3000 })
  const plannerActive = plannerRuns.data?.some((r) => isRunActive(r)) ?? false
  const alerts = useQuery({
    queryKey: ['alerts', 'since', summary?.from_time],
    queryFn: () => listAlerts({ since: summary!.from_time }),
    enabled: summary !== null,
  })
  const changes = summary && alerts.data ? riskChanges(alerts.data, summary.from_time) : null

  const refresh = useCallback(async () => {
    const [s, p] = await Promise.all([getSimState(), getSimProgress()])
    setState(s)
    setProgress(p)
    // Time moved or data was regenerated: every cached list, drawer and calendar is now stale.
    await queryClient.invalidateQueries()
  }, [queryClient])

  useEffect(() => {
    Promise.all([getSimState(), getSimProgress()])
      .then(([s, p]) => {
        setState(s)
        setProgress(p)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  /** Run one action, then reload the clock state and progress table. */
  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      await refresh()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const onAdvance = (days: number) => run(async () => setSummary(await advanceSim(days)))

  const onReset = () => {
    if (!window.confirm('Reset wipes all data and regenerates the year. Continue?')) return
    run(async () => {
      await resetSim()
      setSummary(null)
    })
  }

  const buttonClass =
    'rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50'

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">Simulation</h1>
        <p className="mt-2 text-base text-slate-600">
          Simulated clock: fast-forward time and watch bookings and attendance appear.
        </p>
      </div>

      {error && <div className="rounded-md bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>}

      <section className="rounded-lg border border-slate-200 bg-white p-5">
        {state ? (
          <>
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <div className="text-xl font-semibold">{formatTime(state.current_time)}</div>
              <div className="text-sm text-slate-600">
                {state.percent_year_elapsed}% of the year elapsed
                {state.year_ended && ' (year ended)'}
                {state.seed !== null && ` · seed ${state.seed}`}
              </div>
            </div>
            <div className="mt-3 h-2 w-full rounded-full bg-slate-200">
              <div
                className="h-2 rounded-full bg-slate-900"
                style={{ width: `${state.percent_year_elapsed}%` }}
              />
            </div>
            <div className="mt-3 text-sm text-slate-600">
              {state.totals.active_drivers} active drivers · sessions: {state.totals.sessions_completed} completed,{' '}
              {state.totals.sessions_scheduled} scheduled, {state.totals.sessions_cancelled} cancelled · attended{' '}
              {state.totals.enrollments_attended}, no-shows {state.totals.enrollments_no_show}, booked{' '}
              {state.totals.enrollments_booked}
            </div>
          </>
        ) : (
          <div className="text-sm text-slate-500">Loading…</div>
        )}

        <div className="mt-4 flex flex-wrap gap-3">
          {ADVANCE_BUTTONS.map((b) => (
            <button
              key={b.days}
              className={buttonClass}
              disabled={busy || plannerActive || state?.year_ended}
              onClick={() => onAdvance(b.days)}
            >
              {b.label}
            </button>
          ))}
          <button
            className="rounded-md border border-red-300 px-4 py-2 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            disabled={busy || plannerActive}
            onClick={onReset}
          >
            Reset
          </button>
          {busy && <span className="self-center text-sm text-slate-500">Working…</span>}
          {plannerActive && <span className="self-center text-sm text-amber-700">Planner running, the clock is paused</span>}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Last advance</h2>
        {summary ? (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full divide-y divide-slate-200">
              <tbody className="divide-y divide-slate-100">
                {[
                  ['From', formatTime(summary.from_time)],
                  ['To', formatTime(summary.to_time)],
                  ['Days advanced', summary.days_advanced],
                  ['Sessions completed', summary.sessions_completed],
                  ['Attended', summary.attended],
                  ['No-shows', summary.no_shows],
                  ['New bookings', summary.new_bookings],
                  ['Bookings cancelled', summary.enrollments_cancelled],
                  ['Sessions cancelled', summary.sessions_cancelled],
                  ['Sick events', summary.sick_events],
                  ['Drivers activated', summary.drivers_activated],
                  ['Scenario events', summary.scenario_events_applied.join(', ') || 'none'],
                ].map(([label, value]) => (
                  <tr key={label}>
                    <td className={`${cell} w-56 text-slate-600`}>{label}</td>
                    <td className={`${cell} font-medium`}>{value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-slate-500">Nothing advanced yet. Use the buttons above.</p>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Risk changes this run</h2>
        {!summary ? (
          <p className="text-sm text-slate-500">Advance the clock to see which course risks change.</p>
        ) : !changes ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : changes.opened.length + changes.escalated.length + changes.deescalated.length + changes.resolved.length === 0 ? (
          <p className="text-sm text-slate-500">No alerts were opened, escalated or resolved in this run (alerts are checked at the end of each week).</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full divide-y divide-slate-200">
              <tbody className="divide-y divide-slate-100">
                {(
                  [
                    ['Opened', changes.opened],
                    ['Escalated', changes.escalated],
                    ['De-escalated', changes.deescalated],
                    ['Resolved', changes.resolved],
                  ] as const
                ).map(([label, list]) => (
                  <tr key={label}>
                    <td className={`${cell} w-40 text-slate-600`}>
                      {label} <span className="text-slate-400">({list.length})</span>
                    </td>
                    <td className={cell}>
                      <div className="flex flex-wrap gap-x-4 gap-y-1">
                        {list.map((a) => (
                          <span key={a.id} className="inline-flex items-center gap-1.5">
                            <span className="font-medium">{a.course_code}</span>
                            <RiskBadge level={a.risk_level as 'high' | 'medium'} />
                          </span>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Progress by course</h2>
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full divide-y divide-slate-200">
            <thead className="bg-slate-50">
              <tr>
                {['Course', 'Type', 'Target', 'Attended', '% of target', 'Booked upcoming', 'Capacity remaining'].map(
                  (h) => (
                    <th key={h} className={headCell}>
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {progress.map((c) => (
                <tr key={c.course_id}>
                  <td className={`${cell} font-medium`}>
                    {c.name} <span className="text-slate-400">({c.code})</span>
                  </td>
                  <td className={cell}>{c.is_mandatory ? 'Mandatory' : 'Optional'}</td>
                  <td className={cell}>{c.target}</td>
                  <td className={cell}>{c.attended}</td>
                  <td className={cell}>{c.percent_of_target}%</td>
                  <td className={cell}>{c.booked_upcoming}</td>
                  <td className={cell}>{c.planned_capacity_remaining}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
