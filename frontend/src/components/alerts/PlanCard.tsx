import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import {
  REJECT_REASONS, approvePlan, rejectPlan,
  type Change, type ComponentName, type PlanView, type ScoreBreakdown, type Violation,
} from '../../api/alerts'
import { ApiError } from '../../api/client'
import { cn } from '../../lib/cn'
import { Button, ConfirmDialog, SelectField, Tag, TextAreaField } from '../ui'
import { useToast } from '../Toast'

const PARTS: { key: ComponentName; label: string; bar: string; help: string }[] = [
  { key: 'closure', label: 'Closes the gap', bar: 'bg-primary', help: 'How much of the expected shortfall the plan fills' },
  { key: 'confidence', label: 'Chance of hitting target', bar: 'bg-risk-ok', help: 'Chance of reaching the yearly target after the plan' },
  { key: 'fairness', label: 'Fair spread', bar: 'bg-risk-medium', help: 'Whether the extra work is spread like the eligible drivers' },
  { key: 'disruption', label: 'Few changes', bar: 'bg-ink-muted', help: 'Fewer drivers and sessions touched scores higher' },
  { key: 'resource', label: 'Little trainer time', bar: 'bg-risk-unknown', help: 'Fewer extra trainer hours scores higher' },
]

const pct = (p: number) => `${Math.round(p * 100)}%`

/** One stacked bar: each coloured part is that component's weighted share of the total score. */
function ScoreBar({ breakdown }: { breakdown: ScoreBreakdown }) {
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-canvas" role="img" aria-label="How the score is made up">
        {PARTS.map((p) => (
          <div
            key={p.key}
            className={p.bar}
            style={{ width: `${breakdown.weighted[p.key] * 100}%` }}
            title={`${p.label}: ${breakdown.components[p.key].toFixed(2)} out of 1 (weight ${breakdown.weights[p.key]}). ${p.help}`}
          />
        ))}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
        {PARTS.map((p) => (
          <li key={p.key} className="flex items-center gap-1" title={p.help}>
            <span className={cn('inline-block h-2 w-2 rounded-full', p.bar)} aria-hidden="true" />
            {p.label} {breakdown.components[p.key].toFixed(2)}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ChangeTables({ changes }: { changes: Change[] }) {
  const driverChanges = changes.filter((c): c is Exclude<Change, { type: 'add_session' }> => c.type !== 'add_session')
  const newSessions = changes.filter((c): c is Extract<Change, { type: 'add_session' }> => c.type === 'add_session')
  return (
    <div className="space-y-3">
      {newSessions.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="pb-1 text-left text-xs font-medium text-ink-muted">New sessions</caption>
          <thead className="text-xs text-ink-muted"><tr><th className="py-1 pr-3 font-medium">When</th><th className="pr-3 font-medium">Trainer</th><th className="pr-3 font-medium">Where</th><th className="font-medium">Seats</th></tr></thead>
          <tbody>
            {newSessions.map((s) => (
              <tr key={s.temp_ref} className="border-t border-line"><td className="py-1 pr-3">{s.when}</td><td className="pr-3">{s.trainer_name}</td><td className="pr-3">{s.location}</td><td>{s.capacity}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {driverChanges.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="pb-1 text-left text-xs font-medium text-ink-muted">Driver bookings</caption>
          <thead className="text-xs text-ink-muted"><tr><th className="py-1 pr-3 font-medium">Driver</th><th className="pr-3 font-medium">From</th><th className="font-medium">To</th></tr></thead>
          <tbody>
            {driverChanges.map((c, i) => {
              const to = c.type === 'enroll_driver' ? c.when : c.to_when
              const toLabel = to ?? (typeof (c.type === 'enroll_driver' ? c.session_id : c.to_session_id) === 'string' ? 'New session' : 'Unknown')
              return (
                <tr key={i} className="border-t border-line">
                  <td className="py-1 pr-3">{c.driver_name ?? `Driver ${c.driver_id}`}{c.shift && <span className="text-ink-muted"> · {c.shift}</span>}</td>
                  <td className="pr-3 text-ink-muted">{c.type === 'move_driver' ? c.from_when : 'Not booked'}</td>
                  <td><ArrowRight size={12} className="mr-1 inline text-ink-muted" aria-hidden="true" />{toLabel}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

export function ViolationList({ violations }: { violations: Violation[] }) {
  return (
    <ul className="space-y-1 text-sm">
      {violations.map((v, i) => (
        <li key={i}><code className="rounded bg-canvas px-1 text-xs">{v.code}</code> <span className="text-ink">{v.message}</span></li>
      ))}
    </ul>
  )
}

interface Props {
  plan: PlanView
  actorId: number | null
  isTop: boolean
  canDecide: boolean
  onRegenerate: () => void
}

/** A ranked plan with its score, what it changes, and the Approve / Reject buttons. */
export default function PlanCard({ plan, actorId, isTop, canDecide, onRegenerate }: Props) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [rejecting, setRejecting] = useState(false)
  const [reasonCode, setReasonCode] = useState('not_feasible')
  const [reasonText, setReasonText] = useState('')
  const b = plan.score_breakdown as ScoreBreakdown

  const approve = useMutation({
    mutationFn: () => approvePlan(plan.id, actorId),
    onSuccess: () => {
      toast.success('Plan approved. The changes are saved and queued for the calendar.')
      void queryClient.invalidateQueries() // dashboard, calendar, alerts and records all changed
    },
    onError: () => void queryClient.invalidateQueries({ queryKey: ['alert'] }),
  })
  const reject = useMutation({
    mutationFn: () => rejectPlan(plan.id, { actor_trainer_id: actorId, reason_code: reasonCode, reason_text: reasonText }),
    onSuccess: () => {
      setRejecting(false)
      void queryClient.invalidateQueries({ queryKey: ['alert'] })
      void queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
  })

  const conflict = approve.error instanceof ApiError && approve.error.status === 409 ? approve.error : null
  const violations = (conflict?.body.violations as Violation[] | undefined) ?? []
  const decided = plan.status !== 'valid'

  return (
    <article className={cn('rounded-panel border bg-surface p-4', isTop ? 'border-primary' : 'border-line')} aria-label={`Plan ranked ${plan.rank}`}>
      <header className="flex flex-wrap items-center gap-2">
        <span className="font-heading text-lg font-semibold text-ink">{isTop ? 'Recommended: plan' : 'Plan'} {plan.rank}</span>
        <Tag>Score {plan.score_total?.toFixed(2)}</Tag>
        {plan.status !== 'valid' && <Tag className="capitalize">{plan.status}</Tag>}
        {plan.fairness_flag && (
          <span className="inline-flex items-center gap-1 rounded-full bg-risk-high-bg px-2 py-0.5 text-xs font-medium text-risk-high">
            <TriangleAlert size={12} aria-hidden="true" /> Uneven burden
          </span>
        )}
        {b.p_hit_before !== undefined && (
          <span className="ml-auto text-sm text-ink">
            Chance of hitting the target: <span className="text-ink-muted">{pct(b.p_hit_before)}</span> <ArrowRight size={12} className="inline" aria-hidden="true" /> <span className="font-semibold">{pct(b.p_hit_after)}</span>
          </span>
        )}
      </header>

      {b.components && <div className="mt-3"><ScoreBar breakdown={b} /></div>}

      {(plan.rationale || plan.agent_rationale) && <p className="mt-3 text-sm text-ink">{plan.rationale ?? plan.agent_rationale}</p>}
      {plan.tradeoffs && <p className="mt-1 text-sm text-ink-muted"><span className="font-medium">Trade-off:</span> {plan.tradeoffs}</p>}
      {plan.fairness_flag && <p className="mt-1 text-sm text-risk-high">Most of the extra work falls on one group of drivers, more than their share of the eligible pool.</p>}

      {plan.changes && plan.changes.length > 0 && <div className="mt-3"><ChangeTables changes={plan.changes} /></div>}

      {conflict && (
        <div role="alert" className="mt-3 rounded-control border border-risk-high bg-risk-high-bg p-3 text-sm text-ink">
          <p className="font-medium">{conflict.message}</p>
          {violations.length > 0 && <div className="mt-2"><ViolationList violations={violations} /></div>}
          <Button size="sm" variant="primary" className="mt-2" onClick={onRegenerate}>Make new plans</Button>
        </div>
      )}
      {approve.error && !conflict && <p role="alert" className="mt-3 text-sm text-risk-high">{approve.error.message}</p>}

      {canDecide && !decided && (
        <div className="mt-4 flex gap-2 border-t border-line pt-3">
          <Button variant="primary" disabled={approve.isPending || actorId === null} onClick={() => approve.mutate()}>
            {approve.isPending ? 'Approving…' : 'Approve'}
          </Button>
          <Button disabled={actorId === null} onClick={() => setRejecting(true)}>Reject</Button>
          {actorId === null && <span className="self-center text-xs text-ink-muted">Choose who you are acting as, at the top of the page.</span>}
        </div>
      )}

      <ConfirmDialog
        open={rejecting}
        onOpenChange={setRejecting}
        title={`Reject plan ${plan.rank}?`}
        description="Tell us why, so the next plans can do better. Nothing is changed in the schedule."
        confirmLabel="Reject plan"
        tone="danger"
        busy={reject.isPending}
        onConfirm={() => reject.mutate()}
      >
        <div className="space-y-3">
          <SelectField label="Reason" value={reasonCode} onChange={(e) => setReasonCode(e.target.value)}>
            {Object.entries(REJECT_REASONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </SelectField>
          <TextAreaField
            label="Details"
            required={reasonCode === 'other'}
            value={reasonText}
            onChange={(e) => setReasonText(e.target.value)}
            error={reject.error instanceof ApiError ? reject.error.fieldErrors.reason_text : undefined}
          />
          {reject.error && <p role="alert" className="text-sm text-risk-high">{reject.error.message}</p>}
        </div>
      </ConfirmDialog>
    </article>
  )
}
