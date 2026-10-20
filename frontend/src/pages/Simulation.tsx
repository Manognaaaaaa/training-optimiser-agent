import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { listAlerts, type Alert } from '../api/alerts'
import { getSimProgress, resetSim, type AdvanceSummary } from '../api/sim'
import type { RiskLevel } from '../api/tracking'
import { AutoplayPanel } from '../components/AutoplayPanel'
import { useAdvance } from '../components/shell/useAdvance'
import { Button, ConfirmDialog, DataTable, ErrorState, Hint, PageHeader, Panel, ProgressBar, RiskPill, Skeleton, StatCard, type Column } from '../components/ui'
import { fmtDate, fmtNumber } from '../lib/format'
import { clearAdvanceLog, useAdvanceLog } from '../lib/simLog'
import type { CourseProgress } from '../api/sim'

const STEPS = [
  { label: 'Next day', days: 1 },
  { label: 'Next week', days: 7 },
  { label: 'Next month', days: 30 },
]

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

export default function Simulation() {
  const queryClient = useQueryClient()
  const { state, advance, plannerBusy, disabledReason, disabled } = useAdvance()
  const log = useAdvanceLog()
  const progress = useQuery({ queryKey: ['sim', 'progress'], queryFn: getSimProgress })
  const [confirmReset, setConfirmReset] = useState(false)

  const last: AdvanceSummary | undefined = advance.data
  const sinceTime = last?.from_time
  const alerts = useQuery({ queryKey: ['alerts', 'since', sinceTime], queryFn: () => listAlerts({ since: sinceTime }), enabled: Boolean(sinceTime) })
  const changes = sinceTime && alerts.data ? riskChanges(alerts.data, sinceTime) : null

  const s = state.data
  const columns: Column<CourseProgress>[] = [
    { key: 'course', header: 'Course', cell: (c) => <span className="font-medium">{c.name} <span className="text-ink-muted">({c.code})</span></span>, sortValue: (c) => c.name },
    {
      key: 'progress', header: 'Progress',
      cell: (c) => (
        <div className="w-44">
          <ProgressBar value={c.attended} max={c.target} label={`${c.name} completions`} />
          <div className="mt-1 text-xs text-ink-muted">{fmtNumber(c.attended)} of {fmtNumber(c.target)} ({c.percent_of_target}%)</div>
        </div>
      ),
      sortValue: (c) => c.percent_of_target,
    },
    { key: 'booked', header: 'Booked for later', cell: (c) => fmtNumber(c.booked_upcoming), sortValue: (c) => c.booked_upcoming, align: 'right' },
    { key: 'seats', header: 'Seats still open', cell: (c) => fmtNumber(c.planned_capacity_remaining), sortValue: (c) => c.planned_capacity_remaining, align: 'right' },
  ]

  async function doReset() {
    try {
      await resetSim()
      clearAdvanceLog()
      await queryClient.invalidateQueries()
      toast.success('The simulation was reset to 1 January 2026 with fresh data.')
      setConfirmReset(false)
    } catch (e) {
      toast.error((e as Error).message)
      setConfirmReset(false)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Simulation"
        lead="This app runs on a simulated calendar. Move the clock forward to play out bookings and attendance for those days, then watch forecasts and alerts change."
      />

      {state.isError && <ErrorState title="The simulation could not load" message={state.error.message} onRetry={() => void state.refetch()} />}

      <Panel>
        {s ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-end justify-between gap-4">
              <div>
                <div className="text-sm text-ink-muted">Simulated date</div>
                <div className="font-heading text-3xl font-bold">{fmtDate(s.current_time)}</div>
                <div className="text-sm text-ink-muted">
                  {s.percent_year_elapsed}% of the year has passed{s.year_ended && ' (the year has ended)'}
                  {s.seed !== null && ` · data seed ${s.seed}`}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {STEPS.map((step) => {
                  const button = (
                    <Button variant="primary" disabled={disabled} onClick={() => advance.mutate(step.days)}>
                      {advance.isPending && advance.variables === step.days ? 'Moving…' : step.label}
                    </Button>
                  )
                  return disabledReason ? <Hint key={step.days} text={disabledReason}>{button}</Hint> : <span key={step.days}>{button}</span>
                })}
                <Hint text={plannerBusy ? 'The planner is running.' : 'Wipes all data and starts again from 1 January.'}>
                  <Button variant="danger" disabled={advance.isPending || plannerBusy} onClick={() => setConfirmReset(true)}>Reset</Button>
                </Hint>
              </div>
            </div>
            <div className="h-2 rounded-full bg-line" role="progressbar" aria-label="Share of the year that has passed" aria-valuemin={0} aria-valuemax={100} aria-valuenow={s.percent_year_elapsed}>
              <div className="h-2 rounded-full bg-signal" style={{ width: `${s.percent_year_elapsed}%` }} />
            </div>
            {plannerBusy && <p className="text-sm text-risk-medium">The planner is running, so the clock is paused until it finishes.</p>}
          </div>
        ) : (
          <Skeleton className="h-24 w-full" />
        )}
      </Panel>

      <AutoplayPanel />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard loading={!s} label="Active drivers" value={s ? fmtNumber(s.totals.active_drivers) : ''} />
        <StatCard
          loading={!s} label="Sessions" value={s ? fmtNumber(s.totals.sessions_completed + s.totals.sessions_scheduled) : ''}
          note={s && `${fmtNumber(s.totals.sessions_completed)} held, ${fmtNumber(s.totals.sessions_scheduled)} still to come, ${fmtNumber(s.totals.sessions_cancelled)} cancelled`}
        />
        <StatCard loading={!s} label="Attended" value={s ? fmtNumber(s.totals.enrollments_attended) : ''} note={s && `${fmtNumber(s.totals.enrollments_no_show)} did not show up`} />
        <StatCard loading={!s} label="Booked for later" value={s ? fmtNumber(s.totals.enrollments_booked) : ''} />
      </div>

      <section className="grid gap-6 xl:grid-cols-2">
        <Panel title="Last move">
          {last ? (
            <div className="space-y-2 text-sm">
              <p className="text-base">
                {fmtDate(last.from_time)} to {fmtDate(last.to_time)} ({last.days_advanced} day{last.days_advanced === 1 ? '' : 's'}): {last.sessions_completed} sessions held,{' '}
                {last.attended} attended, {last.no_shows} no-shows, {last.new_bookings} new bookings.
              </p>
              {(last.enrollments_cancelled > 0 || last.sessions_cancelled > 0 || last.sick_events > 0) && (
                <p className="text-ink-muted">
                  Also: {last.enrollments_cancelled} bookings cancelled, {last.sessions_cancelled} sessions cancelled, {last.sick_events} sick days.
                </p>
              )}
              {last.scenario_events_applied.length > 0 && <p className="text-ink-muted">Events: {last.scenario_events_applied.join(', ')}.</p>}
              <div className="border-t border-line pt-2">
                <div className="mb-1 font-medium">What changed for the courses</div>
                {!changes ? (
                  <Skeleton className="h-5 w-48" />
                ) : changes.opened.length + changes.escalated.length + changes.deescalated.length + changes.resolved.length === 0 ? (
                  <p className="text-ink-muted">No alerts opened, got worse or were resolved. Alerts are checked at the end of each week.</p>
                ) : (
                  <ul className="space-y-1">
                    {([['Alert opened', changes.opened], ['Got worse', changes.escalated], ['Got better', changes.deescalated], ['Resolved', changes.resolved]] as const)
                      .filter(([, list]) => list.length > 0)
                      .map(([label, list]) => (
                        <li key={label} className="flex flex-wrap items-center gap-2">
                          <span className="w-24 text-ink-muted">{label}</span>
                          {list.map((a) => (
                            <span key={a.id} className="inline-flex items-center gap-1.5">
                              <Link to={`/dashboard/courses/${a.course_id}`} className="text-primary hover:underline">{a.course_name}</Link>
                              <RiskPill level={a.risk_level as RiskLevel} />
                            </span>
                          ))}
                        </li>
                      ))}
                  </ul>
                )}
              </div>
            </div>
          ) : (
            <p className="text-sm text-ink-muted">Nothing moved yet in this visit. Use the buttons above, or the clock at the top of any page.</p>
          )}
        </Panel>

        <Panel title="Recent moves">
          {log.length === 0 ? (
            <p className="text-sm text-ink-muted">Each time the clock moves, a line appears here (kept in this browser).</p>
          ) : (
            <ol className="divide-y divide-line text-sm">
              {log.map((e, i) => (
                <li key={`${e.to}-${i}`} className="py-2 first:pt-0 last:pb-0">
                  <div className="font-medium">{fmtDate(e.from)} to {fmtDate(e.to)}</div>
                  <div className="text-ink-muted">
                    {e.days} day{e.days === 1 ? '' : 's'} · {e.sessionsCompleted} sessions held · {e.attended} attended · {e.noShows} no-shows · {e.newBookings} new bookings
                  </div>
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </section>

      <section className="space-y-2">
        <h2 className="font-heading text-xl font-semibold">Progress by course</h2>
        {progress.isError ? (
          <ErrorState title="Progress could not load" message={progress.error.message} onRetry={() => void progress.refetch()} />
        ) : progress.isPending ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <DataTable caption="Progress by course" columns={columns} rows={progress.data} rowKey={(c) => c.course_id} initialSort={{ key: 'progress', dir: 'asc' }} />
        )}
      </section>

      <ConfirmDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title="Reset the simulation?"
        description="This wipes all drivers, sessions, bookings and alerts and generates a fresh year starting on 1 January 2026. It cannot be undone."
        confirmLabel="Reset everything"
        tone="danger"
        onConfirm={() => void doReset()}
      />
    </div>
  )
}
