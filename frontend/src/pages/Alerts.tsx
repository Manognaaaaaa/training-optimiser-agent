import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { listAlerts, type Alert } from '../api/alerts'
import { getSimState } from '../api/sim'
import { getTrackingCourses, type RiskLevel } from '../api/tracking'
import PlannerPanel from '../components/PlannerPanel'
import { Button, EmptyState, ErrorState, Hint, PageHeader, Panel, RiskPill, Skeleton } from '../components/ui'
import { cn } from '../lib/cn'
import { PROBLEM_LABEL, RISK_ORDER, courseSummary, fmtDate, whySentence } from '../lib/format'

const STATUS_LABEL: Record<Alert['status'], string> = { open: 'Needs review', resolved: 'Resolved', dismissed: 'Dismissed' }

/** "today", "3 days ago": measured against the simulated date, not the real one. */
function ageText(iso: string | null, simNow: string | undefined): string {
  if (!iso || !simNow) return ''
  const days = Math.floor((new Date(simNow).getTime() - new Date(iso).getTime()) / 86_400_000)
  if (days <= 0) return 'today'
  return days === 1 ? '1 day ago' : `${days} days ago`
}

function AlertRow({ alert, selected, simNow, onSelect }: { alert: Alert; selected: boolean; simNow?: string; onSelect: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? 'true' : undefined}
        className={cn('w-full rounded-panel border p-3 text-left', selected ? 'border-primary bg-primary-soft' : 'border-line bg-surface hover:bg-canvas')}
      >
        <div className="flex items-start justify-between gap-2">
          <span className="font-medium text-ink">{alert.course_name}</span>
          <RiskPill level={alert.risk_level as RiskLevel} />
        </div>
        <div className="mt-1 flex items-center justify-between text-xs text-ink-muted">
          <span>{STATUS_LABEL[alert.status]}</span>
          <span>{ageText(alert.updated_at ?? alert.created_at, simNow)}</span>
        </div>
      </button>
    </li>
  )
}

function Detail({ alert, simNow }: { alert: Alert; simNow?: string }) {
  const courses = useQuery({ queryKey: ['tracking', 'courses'], queryFn: getTrackingCourses })
  const course = courses.data?.find((c) => c.course_id === alert.course_id)

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-heading text-2xl font-semibold text-ink">{alert.course_name}</h2>
          <RiskPill level={alert.risk_level as RiskLevel} />
          <span className="rounded-full bg-risk-unknown-bg px-2.5 py-0.5 text-xs text-ink-muted">{STATUS_LABEL[alert.status]}</span>
        </div>
        <p className="mt-1 text-sm text-ink-muted">
          Opened {fmtDate(alert.created_at)}
          {alert.updated_at && ` · last changed ${ageText(alert.updated_at, simNow)}`} ·{' '}
          <Link to={`/dashboard/courses/${alert.course_id}`} className="text-primary hover:underline">Open course</Link>
        </p>
      </div>

      <Panel title="What is going on">
        {course ? (
          <div className="space-y-2 text-sm text-ink">
            <p className="text-base">{courseSummary(course)}</p>
            <p><span className="font-medium">Problem:</span> {PROBLEM_LABEL[course.shortfall_type]}. {whySentence(course)}</p>
          </div>
        ) : courses.isPending ? (
          <Skeleton className="h-12 w-full" />
        ) : (
          <p className="text-sm text-ink">
            Expected to finish at {alert.projected_completions} of {alert.target_completions}
            {alert.shortfall_type ? `. Problem: ${PROBLEM_LABEL[alert.shortfall_type]}.` : '.'}
          </p>
        )}
        <div className="mt-4 border-t border-line pt-3">
          <div className="text-sm font-medium text-ink">Message for the trainer</div>
          {alert.message ? (
            <p className="mt-1 max-w-3xl whitespace-pre-line text-base text-ink">{alert.message}</p>
          ) : (
            <p className="mt-1 text-sm text-ink-muted">A short written summary for the trainer will appear here once alert messages are generated (a later phase).</p>
          )}
        </div>
      </Panel>

      <PlannerPanel
        courseId={alert.course_id}
        renderPlanFooter={(plan) =>
          plan.outcome === 'valid' && !plan.is_duplicate ? (
            <div className="mt-3 flex gap-2 border-t border-line pt-3">
              <Hint text="Available once ranking is built">
                <Button variant="primary" size="sm" disabled>Approve</Button>
              </Hint>
              <Hint text="Available once ranking is built">
                <Button size="sm" disabled>Reject</Button>
              </Hint>
            </div>
          ) : null
        }
      />
    </div>
  )
}

export default function Alerts() {
  const [params, setParams] = useSearchParams()
  const [showAll, setShowAll] = useState(false)
  const alerts = useQuery({ queryKey: ['alerts', 'all'], queryFn: () => listAlerts() })
  const sim = useQuery({ queryKey: ['sim', 'state'], queryFn: getSimState })

  const list = useMemo(
    () =>
      [...(alerts.data ?? [])]
        .filter((a) => showAll || a.status === 'open')
        .sort(
          (a, b) =>
            Number(b.status === 'open') - Number(a.status === 'open') ||
            RISK_ORDER[a.risk_level as RiskLevel] - RISK_ORDER[b.risk_level as RiskLevel] ||
            (b.updated_at ?? '').localeCompare(a.updated_at ?? ''),
        ),
    [alerts.data, showAll],
  )

  const selectedId = Number(params.get('alert')) || null
  const selected = list.find((a) => a.id === selectedId) ?? (alerts.data ?? []).find((a) => a.id === selectedId) ?? null

  // On wide screens, open the first alert straight away. On phones the list comes first.
  useEffect(() => {
    if (selectedId === null && list.length > 0 && window.matchMedia('(min-width: 1024px)').matches) {
      setParams({ alert: String(list[0].id) }, { replace: true })
    }
  }, [selectedId, list, setParams])

  const openCount = (alerts.data ?? []).filter((a) => a.status === 'open').length

  return (
    <div className="space-y-6">
      <PageHeader
        title="Alerts"
        lead={alerts.isPending ? undefined : openCount === 0 ? 'No alerts need review right now.' : `${openCount} alert${openCount === 1 ? '' : 's'} need review. Pick one to see what is wrong and what could fix it.`}
      />
      {alerts.isError && <ErrorState title="Alerts could not load" message={alerts.error.message} onRetry={() => void alerts.refetch()} />}
      {alerts.isPending && <Skeleton className="h-64 w-full" />}

      {alerts.data && list.length === 0 && (
        <EmptyState title={showAll ? 'No alerts yet' : 'Nothing to review'} action={!showAll ? <Button onClick={() => setShowAll(true)}>Show resolved alerts too</Button> : undefined}>
          Alerts open when a course is likely to miss its yearly target, and close by themselves when it recovers.
        </EmptyState>
      )}

      {alerts.data && list.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
          <div className={cn('space-y-3', selected && 'hidden lg:block')}>
            <div className="flex gap-2" role="group" aria-label="Which alerts to show">
              {[{ label: 'Needs review', all: false }, { label: 'All', all: true }].map((f) => (
                <button
                  key={f.label}
                  type="button"
                  aria-pressed={showAll === f.all}
                  onClick={() => setShowAll(f.all)}
                  className={cn('rounded-full border px-3 py-1 text-sm', showAll === f.all ? 'border-primary bg-primary text-white' : 'border-line bg-surface hover:bg-canvas')}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <ul className="space-y-2">
              {list.map((a) => (
                <AlertRow key={a.id} alert={a} selected={a.id === selected?.id} simNow={sim.data?.current_time} onSelect={() => setParams({ alert: String(a.id) })} />
              ))}
            </ul>
          </div>

          <div className={cn('min-w-0', !selected && 'hidden lg:block')}>
            {selected ? (
              <>
                <Button variant="ghost" size="sm" className="mb-3 lg:hidden" onClick={() => setParams({})}>
                  <ArrowLeft size={14} aria-hidden="true" /> All alerts
                </Button>
                <Detail alert={selected} simNow={sim.data?.current_time} />
              </>
            ) : (
              <EmptyState title="Pick an alert">Choose an alert on the left to see what is wrong and what could fix it.</EmptyState>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
