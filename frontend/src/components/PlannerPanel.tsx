import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { listAlerts } from '../api/alerts'
import { ApiError } from '../api/client'
import {
  getPlannerRun,
  isRunActive,
  listPlannerRuns,
  startPlannerRun,
  type Outcome,
  type PlannerPlan,
} from '../api/planner'
import { Badge } from './Badge'

const OUTCOME_TONE: Record<Outcome, 'green' | 'red' | 'slate' | 'amber'> = {
  valid: 'green',
  invalid: 'red',
  schema_invalid: 'slate',
  no_plan: 'amber',
  step_limit: 'slate',
}
const OUTCOME_LABEL: Record<Outcome, string> = {
  valid: 'valid',
  invalid: 'invalid',
  schema_invalid: 'schema invalid',
  no_plan: 'no plan',
  step_limit: 'step limit',
}
const N_CHOICES = [1, 3, 5, 8, 10]

function PlanCard({ plan }: { plan: PlannerPlan }) {
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-slate-900">Sample {plan.sample_index + 1}</span>
        <Badge tone={OUTCOME_TONE[plan.outcome]}>{OUTCOME_LABEL[plan.outcome]}</Badge>
        {plan.is_duplicate && <Badge tone="blue">duplicate</Badge>}
        <span className="text-xs text-slate-500">
          {plan.strategy} · temperature {plan.temperature} · {plan.n_steps} steps · {plan.total_tokens} tokens
        </span>
      </div>

      {plan.action_labels.length > 0 && (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-sm text-slate-800">
          {plan.action_labels.map((label, i) => (
            <li key={i}>{label}</li>
          ))}
        </ul>
      )}

      {plan.violations.length > 0 && (
        <ul className="mt-2 space-y-0.5 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
          {plan.violations.map((v, i) => (
            <li key={i}>
              <span className="font-mono text-xs">{v.code}</span>: {v.message}
            </li>
          ))}
        </ul>
      )}

      {plan.rationale && <p className="mt-2 text-sm italic text-slate-600">“{plan.rationale}”</p>}
    </li>
  )
}

/** "Generate plans" button, progress and the stored plans for the course's open alert. No approve/reject yet (Phase 6). */
export default function PlannerPanel({ courseId }: { courseId: number }) {
  const queryClient = useQueryClient()
  const [n, setN] = useState(5)
  const [startedRunId, setStartedRunId] = useState<number | null>(null)

  const alert = useQuery({
    queryKey: ['alerts', 'open', courseId],
    queryFn: () => listAlerts({ status: 'open', course_id: courseId }),
  })
  const alertId = alert.data?.[0]?.id

  const runs = useQuery({
    queryKey: ['planner', 'runs', alertId],
    queryFn: () => listPlannerRuns({ alert_id: alertId }),
    enabled: alertId !== undefined,
  })
  const runId = startedRunId ?? runs.data?.[0]?.id ?? null

  const run = useQuery({
    queryKey: ['planner', 'run', runId],
    queryFn: () => getPlannerRun(runId!),
    enabled: runId !== null,
    refetchInterval: (query) => (isRunActive(query.state.data) ? 1500 : false),
  })

  const start = useMutation({
    mutationFn: () => startPlannerRun(alertId!, n),
    onSuccess: (created) => {
      setStartedRunId(created.id)
      queryClient.invalidateQueries({ queryKey: ['planner'] })
    },
  })

  if (alert.isPending || !alertId) return null // no open alert: nothing to plan

  const active = isRunActive(run.data) || start.isPending
  const errorText =
    start.error instanceof ApiError
      ? start.error.status === 503
        ? `The planner is not available: ${start.error.message}`
        : start.error.message
      : start.error?.message

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Plan generator</h2>
        <div className="flex items-center gap-2">
          <label className="text-sm text-slate-600" htmlFor="planner-n">
            Plans
          </label>
          <select
            id="planner-n"
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            value={n}
            disabled={active}
            onChange={(e) => setN(Number(e.target.value))}
          >
            {N_CHOICES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
          <button
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            disabled={active}
            onClick={() => start.mutate()}
          >
            Generate plans
          </button>
        </div>
      </div>

      {errorText && <div className="rounded-md bg-red-50 px-4 py-3 text-sm text-red-700">{errorText}</div>}

      {run.data && (
        <div className="space-y-3">
          <div className="text-sm text-slate-600">
            {isRunActive(run.data) ? (
              <span>
                Sample {Math.min(run.data.samples_done + 1, run.data.n_requested)} of {run.data.n_requested}…
              </span>
            ) : run.data.status === 'failed' ? (
              <span className="text-red-700">Run failed: {run.data.error}</span>
            ) : (
              <span>
                Run {run.data.id} finished: {run.data.n_valid} valid, {run.data.n_invalid} invalid, {run.data.n_schema_invalid} schema
                invalid, {run.data.n_no_plan} no plan{run.data.n_step_limit > 0 && `, ${run.data.n_step_limit} step limit`}
                {run.data.n_duplicates > 0 && `, ${run.data.n_duplicates} duplicate`} · {run.data.model} ·{' '}
                {(run.data.duration_ms / 1000).toFixed(1)}s
              </span>
            )}
            {run.data.status === 'failed' && run.data.plans.length > 0 && <span> (plans already made are kept below)</span>}
          </div>
          {run.data.plans.length > 0 && (
            <ul className="space-y-3">
              {run.data.plans.map((p) => (
                <PlanCard key={p.id} plan={p} />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
