import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { getTrackingCourses, getTrackingSeries, getTrackingSummary, recomputeTracking, type CourseRow } from '../api/tracking'
import { Sparkline } from '../components/charts/Sparkline'
import { Button, DataTable, EmptyState, ErrorState, InfoTip, PageHeader, ProgressBar, RiskPill, Skeleton, StatCard, Tag, type Column } from '../components/ui'
import { useRefresh } from '../hooks/useRefresh'
import { cn } from '../lib/cn'
import {
  PROBLEM_LABEL, RISK_ORDER, dashboardSummary, fmtChance, fmtNumber, fmtPercent, needsAttention, scheduleDelta, shortByText, whySentence,
} from '../lib/format'

type Filter = 'all' | 'attention' | 'mandatory' | 'optional'

const FILTERS: { id: Filter; label: string; test: (c: CourseRow) => boolean }[] = [
  { id: 'all', label: 'All', test: () => true },
  { id: 'attention', label: 'Needs attention', test: (c) => needsAttention(c.risk_level) },
  { id: 'mandatory', label: 'Mandatory', test: (c) => c.is_mandatory },
  { id: 'optional', label: 'Optional', test: (c) => !c.is_mandatory },
]

/** Worst first: by risk level, then the lowest chance of hitting the target. */
const byRiskThenChance = (a: CourseRow, b: CourseRow) => RISK_ORDER[a.risk_level] - RISK_ORDER[b.risk_level] || a.p_hit - b.p_hit

const DELTA_CLASS = { good: 'text-risk-ok', bad: 'text-risk-high', neutral: 'text-ink-muted' }

function Progress({ course }: { course: CourseRow }) {
  return (
    <div className="w-48 max-w-full">
      <ProgressBar value={course.attended} max={course.target} pace={course.target_pace} label={`${course.name} completions`} />
      <div className="mt-1.5 text-xs text-ink-muted">
        {fmtNumber(course.attended)} of {fmtNumber(course.target)}
      </div>
    </div>
  )
}

function VsSchedule({ course }: { course: CourseRow }) {
  const d = scheduleDelta(course.pace_gap)
  return <span className={cn('whitespace-nowrap font-medium', DELTA_CLASS[d.tone])}>{d.text}</span>
}

function Expected({ course }: { course: CourseRow }) {
  return (
    <div>
      <div className="whitespace-nowrap text-ink">
        {fmtNumber(course.projected)} of {fmtNumber(course.target)}
      </div>
      <div className="whitespace-nowrap text-xs text-ink-muted">
        likely {fmtNumber(course.low)} to {fmtNumber(course.high)}
      </div>
    </div>
  )
}

function CourseName({ course }: { course: CourseRow }) {
  return (
    <div>
      <Link to={`/dashboard/courses/${course.course_id}`} className="font-medium text-primary hover:underline" onClick={(e) => e.stopPropagation()}>
        {course.name}
      </Link>
      <div className="mt-0.5 flex items-center gap-2 text-xs text-ink-muted">
        {course.code}
        <Tag>{course.is_mandatory ? 'Mandatory' : 'Optional'}</Tag>
      </div>
    </div>
  )
}

function Details({ course, series }: { course: CourseRow; series: ReturnType<typeof useSeries> }) {
  const points = series.data?.[course.course_id]
  return (
    <div className="grid gap-4 pt-2 lg:grid-cols-[1fr_minmax(0,22rem)]">
      <div className="space-y-3">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
          <dt className="text-ink-muted">Problem</dt>
          <dd className="font-medium text-ink">{PROBLEM_LABEL[course.shortfall_type]}</dd>
          <dt className="text-ink-muted">Why</dt>
          <dd className="text-ink">{whySentence(course)}</dd>
          <dt className="text-ink-muted">Shortfall</dt>
          <dd className="text-ink">{shortByText(course)}</dd>
        </dl>
        <div className="flex flex-wrap gap-2">
          <Link to={`/dashboard/courses/${course.course_id}`}>
            <Button variant="primary" size="sm">Open course</Button>
          </Link>
          {course.open_alert_id !== null && (
            <Link to={`/alerts?alert=${course.open_alert_id}`}>
              <Button size="sm">View alert</Button>
            </Link>
          )}
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs text-ink-muted">Solid: done so far. Dashed: where it should be. Dotted: forecast. Yellow line: today.</div>
        {points ? <Sparkline series={points} target={course.target} /> : <Skeleton className="h-28 w-full" />}
      </div>
    </div>
  )
}

const useSeries = () => useQuery({ queryKey: ['tracking', 'series'], queryFn: getTrackingSeries })

export default function Dashboard() {
  const refresh = useRefresh()
  const summary = useQuery({ queryKey: ['tracking', 'summary'], queryFn: getTrackingSummary })
  const courses = useQuery({ queryKey: ['tracking', 'courses'], queryFn: getTrackingCourses })
  const series = useSeries()
  const [filter, setFilter] = useState<Filter>('all')
  const [expanded, setExpanded] = useState<number | null>(null)

  const recompute = useMutation({
    mutationFn: recomputeTracking,
    onSuccess: async (r) => {
      toast.success(`Forecasts refreshed: ${r.alerts_opened} alert${r.alerts_opened === 1 ? '' : 's'} opened, ${r.alerts_resolved} resolved.`)
      await refresh('sessions')
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const s = summary.data
  const all = useMemo(() => [...(courses.data ?? [])].sort(byRiskThenChance), [courses.data])
  const shown = useMemo(() => all.filter(FILTERS.find((f) => f.id === filter)!.test), [all, filter])
  const attention = all.filter((c) => needsAttention(c.risk_level))
  const high = all.filter((c) => c.risk_level === 'high').length
  const medium = all.filter((c) => c.risk_level === 'medium').length

  // Where the mandatory courses should be today, as a share of their combined target.
  const mandatory = all.filter((c) => c.is_mandatory)
  const mandatoryTarget = mandatory.reduce((sum, c) => sum + c.target, 0)
  const mandatoryPace = mandatoryTarget ? (100 * mandatory.reduce((sum, c) => sum + c.target_pace, 0)) / mandatoryTarget : undefined

  const failed = summary.isError || courses.isError
  const retry = () => {
    void summary.refetch()
    void courses.refetch()
    void series.refetch()
  }
  const loading = summary.isPending || courses.isPending

  const columns: Column<CourseRow>[] = [
    { key: 'course', header: 'Course', cell: (c) => <CourseName course={c} />, sortValue: (c) => c.name },
    {
      key: 'progress',
      header: (
        <span className="flex flex-col font-normal">
          <span className="font-semibold">Progress</span>
          <span>The line shows where the course should be today.</span>
        </span>
      ),
      cell: (c) => <Progress course={c} />,
      sortValue: (c) => c.attended / Math.max(1, c.target),
    },
    {
      key: 'vs', header: 'Vs schedule', info: <InfoTip term="vs_schedule" />,
      cell: (c) => <VsSchedule course={c} />, sortValue: (c) => c.pace_gap,
    },
    {
      key: 'expected', header: 'Expected by 31 Dec', info: <InfoTip term="expected_by" />,
      cell: (c) => <Expected course={c} />, sortValue: (c) => c.projected / Math.max(1, c.target),
    },
    {
      key: 'chance', header: 'Chance of hitting target', info: <InfoTip term="chance" />,
      cell: (c) => <span className="whitespace-nowrap">{fmtChance(c.p_hit)}</span>, sortValue: (c) => c.p_hit,
    },
    { key: 'status', header: 'Status', cell: (c) => <RiskPill level={c.risk_level} />, sortValue: (c) => RISK_ORDER[c.risk_level] },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        lead={loading ? <Skeleton className="mt-1 h-5 w-2/3" /> : failed ? undefined : dashboardSummary(all, s?.open_alerts ?? 0)}
        actions={
          <>
            {s && s.open_alerts > 0 && (
              <Link to="/alerts">
                <Button variant="primary">Review alerts</Button>
              </Link>
            )}
            <Button disabled={recompute.isPending} onClick={() => recompute.mutate()} title="Re-run the forecasts and alert rules now (use after manual edits)">
              {recompute.isPending ? 'Refreshing…' : 'Refresh forecasts'}
            </Button>
          </>
        }
      />

      {failed && <ErrorState title="The dashboard could not load" message={(summary.error ?? courses.error)?.message ?? 'Unknown error'} onRetry={retry} />}

      {!failed && s && !s.has_data ? (
        <EmptyState
          title="Not enough data yet"
          action={<Link to="/simulation"><Button variant="primary">Open the simulation</Button></Link>}
        >
          No training session has been completed yet, so there is nothing to track or forecast from. Move the simulated clock forward to see progress.
        </EmptyState>
      ) : (
        !failed && (
          <>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <StatCard
                loading={loading}
                label="Mandatory courses completed"
                info={<InfoTip term="mandatory" />}
                value={s ? fmtPercent(s.mandatory_compliance_pct, 1) : ''}
                note={
                  <div className="space-y-2">
                    <div>of this year's mandatory target</div>
                    <ProgressBar value={s?.mandatory_compliance_pct ?? 0} max={100} pace={mandatoryPace} label="Mandatory courses completed" />
                  </div>
                }
              />
              <StatCard
                loading={loading}
                label="Completions so far"
                value={s ? <>{fmtNumber(s.total_attended)} <span className="text-xl font-normal text-ink-muted">of {fmtNumber(s.total_target)}</span></> : ''}
                note={s && `${Math.round(s.percent_year_elapsed)}% of the year has passed`}
              />
              <StatCard
                loading={loading}
                label="Courses needing attention"
                value={attention.length}
                note={
                  <span>
                    <span className={high > 0 ? 'font-medium text-risk-high' : undefined}>{high} will likely miss</span>
                    {' · '}
                    <span className={medium > 0 ? 'font-medium text-risk-medium' : undefined}>{medium} at risk</span>
                  </span>
                }
              />
              <StatCard
                loading={loading}
                label="Open alerts"
                value={s?.open_alerts ?? 0}
                note={<Link to="/alerts" className="text-primary hover:underline">Review alerts</Link>}
              />
            </div>

            <section aria-labelledby="course-list-title" className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h2 id="course-list-title" className="font-heading text-xl font-semibold">Courses</h2>
                <div className="flex flex-wrap gap-2" role="group" aria-label="Filter courses">
                  {FILTERS.map((f) => {
                    const count = all.filter(f.test).length
                    const active = filter === f.id
                    return (
                      <button
                        key={f.id}
                        type="button"
                        aria-pressed={active}
                        onClick={() => setFilter(f.id)}
                        className={cn(
                          'rounded-full border px-3 py-1 text-sm',
                          active ? 'border-primary bg-primary text-white' : 'border-line bg-surface text-ink hover:bg-canvas',
                        )}
                      >
                        {f.label} <span className={active ? 'text-white/80' : 'text-ink-muted'}>{count}</span>
                      </button>
                    )
                  })}
                </div>
              </div>

              {courses.isPending ? (
                <div className="space-y-2">
                  {[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-16 w-full" />)}
                </div>
              ) : (
                <DataTable
                  caption="Courses, worst first"
                  columns={columns}
                  rows={shown}
                  rowKey={(c) => c.course_id}
                  expandedKey={expanded}
                  onRowClick={(c) => setExpanded((cur) => (cur === c.course_id ? null : c.course_id))}
                  renderExpanded={(c) => <Details course={c} series={series} />}
                  empty={<EmptyState title="No courses match this filter">Choose another filter above to see more courses.</EmptyState>}
                  mobileCard={(c) => (
                    <div className="space-y-2">
                      <div className="flex items-start justify-between gap-2">
                        <CourseName course={c} />
                        <RiskPill level={c.risk_level} />
                      </div>
                      <Progress course={c} />
                      <div className="flex justify-between text-sm">
                        <VsSchedule course={c} />
                        <span className="text-ink-muted">{fmtChance(c.p_hit)}</span>
                      </div>
                      <Expected course={c} />
                      {expanded === c.course_id && <Details course={c} series={series} />}
                    </div>
                  )}
                />
              )}
            </section>
          </>
        )
      )}
    </div>
  )
}
