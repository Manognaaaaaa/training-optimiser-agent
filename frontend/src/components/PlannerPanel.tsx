import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CircleDot } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ACTIVE_STATUSES, listAlerts } from '../api/alerts'
import { ApiError } from '../api/client'
import {
  getPlannerRun, isRunActive, listPlannerRuns, startPlannerRun, type Outcome, type PlannerPlan, type PlannerRunDetail,
} from '../api/planner'
import { cn } from '../lib/cn'
import { Button, EmptyState, ErrorState, Panel, Skeleton } from './ui'

const N_CHOICES = [1, 3, 5, 8, 10]

/** Plain-language wording for each result, so nobody has to know the internal names. */
const OUTCOME: Record<Outcome, { title: string; help: string; border: string; pill: string }> = {
  valid: { title: 'Safe to use', help: 'This plan follows every rule.', border: 'border-l-risk-ok', pill: 'bg-risk-ok-bg text-risk-ok' },
  invalid: { title: 'Rejected: breaks a rule', help: 'The rule checker found a problem, so this plan cannot be used as it is.', border: 'border-l-risk-high', pill: 'bg-risk-high-bg text-risk-high' },
  schema_invalid: { title: 'Rejected: unreadable answer', help: 'The AI answered in a format the system could not read.', border: 'border-l-risk-unknown', pill: 'bg-risk-unknown-bg text-risk-unknown' },
  no_plan: { title: 'AI found no fix', help: 'The AI looked and said it could not find a valid way to close the gap.', border: 'border-l-risk-medium', pill: 'bg-risk-medium-bg text-risk-medium' },
  step_limit: { title: 'AI ran out of steps', help: 'The AI did not finish in time, so there is no plan from this attempt.', border: 'border-l-risk-unknown', pill: 'bg-risk-unknown-bg text-risk-unknown' },
}

function PlanCard({ plan, index, footer }: { plan: PlannerPlan; index: number; footer?: ReactNode }) {
  const o = OUTCOME[plan.outcome]
  return (
    <li className={cn('rounded-panel border border-l-4 border-line bg-surface p-4', o.border)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-heading text-xl font-semibold text-ink">Plan {index + 1}</span>
        <span className={cn('rounded-full px-2.5 py-0.5 text-xs font-medium', o.pill)}>{o.title}</span>
        {plan.is_duplicate && <span className="rounded-full bg-risk-unknown-bg px-2.5 py-0.5 text-xs text-ink-muted">Same as an earlier plan</span>}
      </div>
      <p className="mt-1 text-xs text-ink-muted">{o.help}</p>

      {plan.action_labels.length > 0 && (
        <div className="mt-3">
          <div className="text-sm font-medium text-ink">What this plan would do</div>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink">
            {plan.action_labels.map((label, i) => <li key={i}>{label}</li>)}
          </ul>
        </div>
      )}
      {plan.rationale && (
        <div className="mt-3">
          <div className="text-sm font-medium text-ink">Why the AI suggests it</div>
          <p className="mt-1 text-sm text-ink">{plan.rationale}</p>
        </div>
      )}
      {plan.violations.length > 0 && (
        <div className="mt-3 rounded-control bg-risk-high-bg px-3 py-2">
          <div className="text-sm font-medium text-risk-high">Why it was rejected</div>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink">
            {plan.violations.map((v, i) => <li key={i}>{v.message}</li>)}
          </ul>
        </div>
      )}
      <details className="mt-3 text-xs text-ink-muted">
        <summary className="cursor-pointer select-none">Technical details</summary>
        <div className="mt-1 space-y-0.5">
          <div>Approach tried: {plan.strategy}</div>
          <div>
            AI creativity setting (temperature): {plan.temperature} · {plan.n_steps} steps · {plan.total_tokens} tokens · {(plan.latency_ms / 1000).toFixed(0)}s
          </div>
          {plan.violations.length > 0 && <div>Rule codes: {plan.violations.map((v) => v.code).join(', ')}</div>}
          {plan.fabricated_ids.length > 0 && <div>The AI invented {plan.fabricated_ids.length} ID(s) that do not exist.</div>}
        </div>
      </details>
      {footer}
    </li>
  )
}

/** Honest progress: the API reports "plan k of N", so the steps are queued, drafting (with the count) and done. */
function Stages({ run }: { run: PlannerRunDetail }) {
  const queued = run.status === 'queued'
  const working = run.status === 'running'
  const stages = [
    { label: 'Waiting to start', state: queued ? 'now' : 'done' },
    { label: `Drafting and checking plans (${Math.min(run.samples_done, run.n_requested)} of ${run.n_requested} done)`, state: queued ? 'later' : working ? 'now' : 'done' },
    { label: 'Done', state: run.status === 'done' ? 'done' : 'later' },
  ] as const
  return (
    <ol className="space-y-1.5" aria-label="Progress">
      {stages.map((s) => (
        <li key={s.label} className={cn('flex items-center gap-2 text-sm', s.state === 'later' ? 'text-ink-muted' : 'text-ink', s.state === 'now' && 'font-medium')}>
          {s.state === 'done' ? <Check size={16} className="text-risk-ok" aria-hidden="true" /> : <CircleDot size={16} className={s.state === 'now' ? 'text-primary' : 'text-line'} aria-hidden="true" />}
          {s.label}
        </li>
      ))}
    </ol>
  )
}

function Summary({ run }: { run: PlannerRunDetail }) {
  const safe = run.plans.filter((p) => p.outcome === 'valid' && !p.is_duplicate).length
  const rejected = run.plans.filter((p) => p.outcome === 'invalid' || p.outcome === 'schema_invalid').length

  if (run.status === 'failed') {
    return <ErrorState title="The AI stopped before finishing" message={`${run.error ?? 'Unknown error'}${run.plans.length > 0 ? ' The plans it finished before stopping are shown below.' : ''}`} />
  }
  if (isRunActive(run)) {
    return (
      <div className="space-y-3 rounded-panel bg-primary-soft px-4 py-3">
        <Stages run={run} />
        <p className="text-xs text-ink-muted">
          Each plan can take from a few seconds to a few minutes, depending on the AI model. Results appear below as they finish. The simulation clock is paused until this is done.
        </p>
      </div>
    )
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-panel bg-canvas px-4 py-3 text-sm text-ink">
      <span>
        <span className="font-medium">Done.</span> The AI made {run.plans.length} plan{run.plans.length === 1 ? '' : 's'}:{' '}
        <span className="font-medium text-risk-ok">{safe} safe to use</span>
        {rejected > 0 && <>, <span className="font-medium text-risk-high">{rejected} rejected</span></>}
        {run.n_no_plan > 0 && <>, {run.n_no_plan} where it found no fix</>}.
      </span>
      <Link to={`/alerts?alert=${run.alert_id}`} className="font-medium text-primary hover:underline">Open this alert</Link>
    </div>
  )
}

/** Plan generator for the course's open alert. Approving a plan arrives in a later phase. */
export default function PlannerPanel({ courseId, renderPlanFooter }: { courseId: number; renderPlanFooter?: (plan: PlannerPlan) => ReactNode }) {
  const queryClient = useQueryClient()
  const [n, setN] = useState(3)
  const [startedRunId, setStartedRunId] = useState<number | null>(null)

  const alert = useQuery({ queryKey: ['alerts', 'open', courseId], queryFn: () => listAlerts({ status: ACTIVE_STATUSES, course_id: courseId }) })
  const alertId = alert.data?.[0]?.id
  const runs = useQuery({ queryKey: ['planner', 'runs', alertId], queryFn: () => listPlannerRuns({ alert_id: alertId }), enabled: alertId !== undefined })
  const runId = startedRunId ?? runs.data?.[0]?.id ?? null
  const run = useQuery({
    queryKey: ['planner', 'run', runId], queryFn: () => getPlannerRun(runId!), enabled: runId !== null,
  }) // progress arrives as live events written into this cache entry, so there is no polling
  const start = useMutation({
    mutationFn: () => startPlannerRun(alertId!, n),
    onSuccess: (created) => {
      setStartedRunId(created.id)
      void queryClient.invalidateQueries({ queryKey: ['planner'] })
    },
  })

  if (alert.isPending) return <Skeleton className="h-32 w-full" />
  if (alert.isError) return <ErrorState title="Could not check this course's alerts" message={alert.error.message} onRetry={() => void alert.refetch()} />
  if (!alertId) {
    return (
      <EmptyState title="No fix needed right now">This course has no open alert. Fix suggestions appear here when a course is likely to miss its target.</EmptyState>
    )
  }

  const active = isRunActive(run.data) || start.isPending
  const errorText =
    start.error instanceof ApiError && start.error.status === 503
      ? 'The AI is not set up yet. Add an AI key in backend/.env and restart the backend (see the README).'
      : start.error?.message

  return (
    <Panel title="Fix suggestions">
      <div className="space-y-4">
        <p className="max-w-3xl text-sm text-ink">
          This course is at risk of missing its target. Ask the AI for different ways to fix it, such as adding sessions or booking more drivers into sessions that have free seats. Every suggestion is checked against the training rules before you see it.{' '}
          <span className="font-medium">Green means safe to use.</span> Nothing changes in your calendar until a plan is approved.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm text-ink" htmlFor="planner-n">How many different plans?</label>
          <select id="planner-n" className="rounded-control border border-line bg-surface px-2 py-1.5 text-sm" value={n} disabled={active} onChange={(e) => setN(Number(e.target.value))}>
            {N_CHOICES.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          <Button variant="primary" disabled={active} onClick={() => start.mutate()}>{active ? 'Working…' : 'Suggest fixes'}</Button>
          <span className="text-xs text-ink-muted">Fewer plans finish faster.</span>
        </div>
        {errorText && <ErrorState title="Could not start" message={errorText} />}
        {run.data && (
          <div className="space-y-3">
            <Summary run={run.data} />
            {run.data.plans.length > 0 && (
              <ul className="space-y-3">
                {[...run.data.plans]
                  .sort((a, b) => Number(b.outcome === 'valid') - Number(a.outcome === 'valid') || a.sample_index - b.sample_index)
                  .map((p, i) => <PlanCard key={p.id} plan={p} index={i} footer={renderPlanFooter?.(p)} />)}
              </ul>
            )}
          </div>
        )}
      </div>
    </Panel>
  )
}
