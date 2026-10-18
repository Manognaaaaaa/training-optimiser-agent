import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { getCourseDetail, type Reason, type UpcomingSession } from '../api/tracking'
import CourseChart from '../components/charts/CourseChart'
import ForecastHistory from '../components/charts/ForecastHistory'
import GroupBars from '../components/charts/GroupBars'
import WeeklyBars from '../components/charts/WeeklyBars'
import PlannerPanel from '../components/PlannerPanel'
import SessionDrawer from '../components/SessionDrawer'
import { DataTable, EmptyState, ErrorState, InfoTip, PageHeader, Panel, ProgressBar, RiskPill, Skeleton, Tag } from '../components/ui'
import type { GlossaryTerm } from '../lib/glossary'
import {
  PROBLEM_LABEL, courseSummary, fmtChance, fmtDateTime, fmtNumber, scheduleDelta, shortByText,
} from '../lib/format'
import { cn } from '../lib/cn'

function Fact({ label, value, term, tone }: { label: string; value: string; term?: GlossaryTerm; tone?: 'good' | 'bad' }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-line py-2 last:border-0">
      <dt className="flex items-center gap-1.5 text-ink-muted">
        {label}
        {term && <InfoTip term={term} />}
      </dt>
      <dd className={cn('text-right font-medium text-ink', tone === 'good' && 'text-risk-ok', tone === 'bad' && 'text-risk-high')}>{value}</dd>
    </div>
  )
}

function ReasonsPanel({ reasons }: { reasons: Reason[] }) {
  return (
    <Panel title="Why this course is at risk">
      {reasons.length === 0 ? (
        <p className="text-sm text-ink-muted">No risk reasons: the course is on track, the target is met, or there is not enough data yet.</p>
      ) : (
        <ol className="divide-y divide-line">
          {reasons.map((r, i) => (
            <li key={r.code} className="flex items-start justify-between gap-4 py-2 first:pt-0 last:pb-0">
              <span className="text-sm text-ink">{i + 1}. {r.message}</span>
              <span className="shrink-0 text-right text-xs text-ink-muted">
                now {Number(r.value.toFixed(1))}, expected {Number(r.benchmark.toFixed(1))}
              </span>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  )
}

export default function CourseDetail() {
  const { id } = useParams()
  const courseId = Number(id)
  const [sessionId, setSessionId] = useState<number | null>(null)
  const detail = useQuery({ queryKey: ['tracking', 'course', courseId], queryFn: () => getCourseDetail(courseId), enabled: Number.isFinite(courseId) })

  if (detail.isPending) {
    return (
      <div className="space-y-6" aria-busy="true">
        <Skeleton className="h-10 w-1/2" />
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-80 w-full" />
      </div>
    )
  }
  if (detail.isError) {
    return (
      <div className="space-y-4">
        <PageHeader title="Course" breadcrumb={[{ label: 'Dashboard', to: '/' }, { label: 'Course' }]} />
        <ErrorState title="This course could not load" message={detail.error.message} onRetry={() => void detail.refetch()} />
      </div>
    )
  }

  const d = detail.data
  const c = d.course
  const delta = scheduleDelta(c.pace_gap)

  const sessionColumns = [
    { key: 'start', header: 'When', cell: (s: UpcomingSession) => fmtDateTime(s.start_time), sortValue: (s: UpcomingSession) => s.start_time },
    { key: 'seats', header: 'Seats taken', cell: (s: UpcomingSession) => `${s.booked} of ${s.capacity}`, sortValue: (s: UpcomingSession) => s.booked },
    {
      key: 'fill', header: 'How full',
      cell: (s: UpcomingSession) => (
        <div className="flex w-44 items-center gap-2">
          <ProgressBar className="flex-1" value={s.booked} max={s.capacity} label="Seats taken" barClassName={s.fill_band === 'low' ? 'bg-risk-medium' : undefined} />
          <span className="w-9 text-right text-xs text-ink-muted">{s.fill_rate === null ? '' : `${Math.round(s.fill_rate * 100)}%`}</span>
        </div>
      ),
      sortValue: (s: UpcomingSession) => s.fill_rate ?? 0,
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title={c.name}
        breadcrumb={[{ label: 'Dashboard', to: '/' }, { label: c.name }]}
        lead={courseSummary(c)}
      />
      <div className="-mt-3 flex flex-wrap items-center gap-2">
        <RiskPill level={c.risk_level} />
        <Tag>{c.is_mandatory ? 'Mandatory' : 'Optional'}</Tag>
        <span className="text-xs text-ink-muted">{c.code}</span>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-6">
          <CourseChart series={d.series} target={c.target} events={d.events} />
          <ReasonsPanel reasons={c.reasons} />
          <PlannerPanel courseId={courseId} />
        </div>

        <aside className="space-y-6">
          <Panel title="Key facts">
            <dl className="text-sm">
              <Fact label="Done so far" value={`${fmtNumber(c.attended)} of ${fmtNumber(c.target)}`} />
              <Fact label="Vs schedule" term="vs_schedule" value={delta.text} tone={delta.tone === 'neutral' ? undefined : delta.tone} />
              <Fact label="Chance of hitting target" term="chance" value={fmtChance(c.p_hit)} />
              <Fact label="Shortfall" term="shortfall" value={shortByText(c)} />
              <Fact label="Problem" term="problem" value={PROBLEM_LABEL[c.shortfall_type]} />
              <Fact label="Seats left" term="seats_left" value={`${fmtNumber(c.remaining_seats)} in ${c.remaining_sessions} sessions`} />
              <Fact label="Show-up rate" term="show_rate" value={`${Math.round(c.p_show * 100)}%`} />
              <Fact label="Eligible drivers" term="eligible_pool" value={fmtNumber(c.eligible_pool)} />
            </dl>
          </Panel>
        </aside>
      </div>

      <section className="space-y-2">
        <h2 className="font-heading text-xl font-semibold">Upcoming sessions</h2>
        <DataTable
          caption="Upcoming sessions for this course"
          columns={sessionColumns}
          rows={d.upcoming_sessions}
          rowKey={(s) => s.id}
          onRowClick={(s) => setSessionId(s.id)}
          initialSort={{ key: 'start', dir: 'asc' }}
          empty={<EmptyState title="No sessions left">There are no sessions left on the calendar for this course.</EmptyState>}
        />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <GroupBars title="Who is behind: by shift" rows={d.breakdown.shift} />
        <GroupBars title="Who is behind: by nationality" rows={d.breakdown.nationality} />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <ForecastHistory snapshots={d.snapshots} target={c.target} events={d.events} />
        <WeeklyBars weeks={d.weekly} />
      </div>

      {sessionId !== null && <SessionDrawer sessionId={sessionId} onClose={() => setSessionId(null)} />}
    </div>
  )
}
