import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, BadgeCheck, FileText } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  REJECT_REASONS, dismissAlert, generatePlans, getAlert, listAlerts, regeneratePlans, rejectAlert,
  type Alert, type AlertDetail, type AlertStatus,
} from '../api/alerts'
import { ApiError } from '../api/client'
import { getSimState } from '../api/sim'
import type { RiskLevel } from '../api/tracking'
import PlanCard, { ViolationList } from '../components/alerts/PlanCard'
import { useToast } from '../components/Toast'
import { Button, ConfirmDialog, EmptyState, ErrorState, PageHeader, Panel, RiskPill, SelectField, Skeleton, Tag, TextAreaField } from '../components/ui'
import { useAllTrainers } from '../hooks/useLookups'
import { cn } from '../lib/cn'
import { PROBLEM_LABEL, fmtChance, fmtDate } from '../lib/format'

const STATUS_LABEL: Record<AlertStatus, string> = {
  open: 'Needs plans', generating: 'Making plans…', plans_ready: 'Plans ready', no_valid_plans: 'No safe plan found',
  approved: 'Approved', rejected: 'Rejected', dismissed: 'Dismissed', resolved: 'Recovered', expired: 'Expired', superseded: 'Replaced',
}

const TABS: { key: string; label: string; statuses: AlertStatus[] }[] = [
  { key: 'open', label: 'Open', statuses: ['open', 'generating', 'no_valid_plans'] },
  { key: 'ready', label: 'Plans ready', statuses: ['plans_ready'] },
  { key: 'approved', label: 'Approved', statuses: ['approved'] },
  { key: 'rejected', label: 'Rejected', statuses: ['rejected', 'dismissed'] },
  { key: 'all', label: 'All', statuses: [] },
]
const RISK_RANK: Record<string, number> = { high: 0, medium: 1 }
const ACTOR_KEY = 'acting-trainer'

function useActor(): [number | null, (id: number | null) => void] {
  const [actor, setActor] = useState<number | null>(() => {
    try {
      const saved = localStorage.getItem(ACTOR_KEY)
      return saved ? Number(saved) : null
    } catch {
      return null
    }
  })
  const update = (id: number | null) => {
    setActor(id)
    try {
      if (id === null) localStorage.removeItem(ACTOR_KEY)
      else localStorage.setItem(ACTOR_KEY, String(id))
    } catch {
      /* storage can be blocked; the choice then lasts until the page is closed */
    }
  }
  return [actor, update]
}

function ageText(iso: string | null, simNow: string | undefined): string {
  if (!iso || !simNow) return ''
  const days = Math.floor((new Date(simNow).getTime() - new Date(iso).getTime()) / 86_400_000)
  if (days <= 0) return 'today'
  return days === 1 ? '1 day ago' : `${days} days ago`
}

function AlertRow({ alert, selected, onSelect }: { alert: Alert; selected: boolean; onSelect: () => void }) {
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
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-muted">
          {alert.shortfall_type && alert.shortfall_type !== 'none' && <Tag>{PROBLEM_LABEL[alert.shortfall_type]}</Tag>}
          {alert.gap > 0 && <span>Short by about {alert.gap}</span>}
        </div>
        <div className="mt-1 flex items-center justify-between text-xs text-ink-muted">
          <span className={cn(alert.status === 'plans_ready' && 'font-medium text-primary')}>{STATUS_LABEL[alert.status]}</span>
          <span>Raised {fmtDate(alert.created_at)}</span>
        </div>
      </button>
    </li>
  )
}

function MessageCard({ detail }: { detail: AlertDetail }) {
  const m = detail.message
  const text = m?.message
  if (!m || !text?.headline) {
    return <p className="text-sm text-ink-muted">A written summary appears here once plans have been made.</p>
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-heading text-lg font-semibold text-ink">{text.headline}</h3>
        {m.used_fallback ? (
          <Tag title="The AI text did not pass the fact check, so a plain template was used">Template message</Tag>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full bg-risk-ok-bg px-2 py-0.5 text-xs font-medium text-risk-ok">
            <BadgeCheck size={12} aria-hidden="true" /> Checked against data
          </span>
        )}
      </div>
      <p className="max-w-3xl text-base text-ink">{text.summary}</p>
      {text.why_at_risk && text.why_at_risk.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-sm text-ink">{text.why_at_risk.map((r, i) => <li key={i}>{r}</li>)}</ul>
      )}
      {text.caveats && text.caveats.length > 0 && <p className="text-sm text-ink-muted">{text.caveats.join(' ')}</p>}
    </div>
  )
}

function ForecastStrip({ detail }: { detail: AlertDetail }) {
  const f = detail.forecast
  const cells = [
    { label: 'Yearly target', value: f.target },
    { label: 'Done so far', value: f.attended ?? '–' },
    { label: 'Expected by year end', value: f.low !== null && f.high !== null ? `${Math.round(f.projected)} (${Math.round(f.low)} to ${Math.round(f.high)})` : Math.round(f.projected) },
    { label: 'Chance of hitting target', value: f.p_hit !== null ? fmtChance(f.p_hit) : '–' },
  ]
  return (
    <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {cells.map((c) => (
        <div key={c.label} className="rounded-panel border border-line bg-surface p-3">
          <dt className="text-xs text-ink-muted">{c.label}</dt>
          <dd className="mt-0.5 text-base font-semibold text-ink">{c.value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Detail({ alertId, simNow, actorId }: { alertId: number; simNow?: string; actorId: number | null }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [n, setN] = useState(3)
  const [rejecting, setRejecting] = useState(false)
  const [reasonCode, setReasonCode] = useState('not_feasible')
  const [reasonText, setReasonText] = useState('')

  const detail = useQuery({
    queryKey: ['alert', alertId],
    queryFn: () => getAlert(alertId),
    refetchInterval: (q) => (q.state.data?.alert.status === 'generating' ? 2000 : false),
  })
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['alert'] })
    void queryClient.invalidateQueries({ queryKey: ['alerts'] })
  }
  const generate = useMutation({
    mutationFn: (again: boolean) => (again ? regeneratePlans(alertId, n) : generatePlans(alertId, n)),
    onSuccess: refresh,
  })
  const dismiss = useMutation({ mutationFn: () => dismissAlert(alertId, actorId), onSuccess: () => { toast.success('Alert dismissed.'); refresh() } })
  const rejectAll = useMutation({
    mutationFn: () => rejectAlert(alertId, { actor_trainer_id: actorId, reason_code: reasonCode, reason_text: reasonText }),
    onSuccess: () => { setRejecting(false); refresh() },
  })

  if (detail.isPending) return <Skeleton className="h-64 w-full" />
  if (detail.isError) return <ErrorState title="This alert could not load" message={detail.error.message} onRetry={() => void detail.refetch()} />
  const d = detail.data
  const { alert } = d
  const status = alert.status
  const canGenerate = status === 'open' || status === 'no_valid_plans'
  const canDecide = status === 'plans_ready'
  const run = d.run
  const generateError = generate.error instanceof ApiError && generate.error.status === 503
    ? 'The AI is not set up yet. Add an AI key in backend/.env and restart the backend (see the README).'
    : generate.error?.message

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-heading text-2xl font-semibold text-ink">{alert.course_name}</h2>
          <RiskPill level={alert.risk_level as RiskLevel} />
          <Tag>{STATUS_LABEL[status]}</Tag>
        </div>
        <p className="mt-1 text-sm text-ink-muted">
          Raised {fmtDate(alert.created_at)}
          {alert.updated_at && ` · last changed ${ageText(alert.updated_at, simNow)}`}
          {alert.shortfall_type && alert.shortfall_type !== 'none' && ` · ${PROBLEM_LABEL[alert.shortfall_type]}`} ·{' '}
          <Link to={`/dashboard/courses/${alert.course_id}`} className="text-primary hover:underline">Open course</Link>
        </p>
      </div>

      <Panel title="What is going on">
        <div className="space-y-4">
          <MessageCard detail={d} />
          <ForecastStrip detail={d} />
        </div>
      </Panel>

      {status === 'generating' && (
        <div role="status" className="rounded-panel bg-primary-soft px-4 py-3 text-sm text-ink">
          <span className="font-medium">Making plans…</span> {run ? `${run.samples_done} of ${run.n_requested} done.` : ''} Each plan can take a few seconds to a few minutes. This page updates by itself.
        </div>
      )}

      {(canGenerate || canDecide) && (
        <div className="flex flex-wrap items-center gap-3">
          <label htmlFor="n-plans" className="text-sm text-ink">How many plans to try?</label>
          <select id="n-plans" className="rounded-control border border-line bg-surface px-2 py-1.5 text-sm" value={n} onChange={(e) => setN(Number(e.target.value))}>
            {[1, 2, 3, 5].map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          <Button variant={canGenerate ? 'primary' : 'secondary'} disabled={generate.isPending} onClick={() => generate.mutate(canDecide)}>
            {canDecide ? 'Make new plans' : 'Make plans'}
          </Button>
          {(canGenerate || canDecide) && <Button variant="ghost" onClick={() => dismiss.mutate()} disabled={dismiss.isPending || actorId === null}>Dismiss alert</Button>}
          {canDecide && <Button variant="ghost" onClick={() => setRejecting(true)} disabled={actorId === null}>Reject all</Button>}
        </div>
      )}
      {generateError && <ErrorState title="Could not start" message={generateError} />}

      {d.presented.length > 0 && (
        <section className="space-y-3" aria-label="Recommended plans">
          <h3 className="font-heading text-xl font-semibold text-ink">Plans, best first</h3>
          {d.presented.map((p) => (
            <PlanCard key={p.id} plan={p} actorId={actorId} isTop={p.rank === 1} canDecide={canDecide} onRegenerate={() => generate.mutate(true)} />
          ))}
        </section>
      )}

      {status === 'no_valid_plans' && (
        <EmptyState title={run?.infeasibility ? 'No valid plan can exist right now' : 'No safe plan was found'}>
          {run?.infeasibility ? (
            <span>{run.infeasibility.message} The AI was not asked, because there was nothing valid to find.</span>
          ) : (
            <span>The AI's ideas all broke a training rule or it found no fix. You can try again, or dismiss the alert.</span>
          )}
        </EmptyState>
      )}

      {d.other_valid.length > 0 && (
        <details className="rounded-panel border border-line bg-surface p-4">
          <summary className="cursor-pointer text-sm font-medium text-ink">Other valid plans ({d.other_valid.length})</summary>
          <div className="mt-3 space-y-3">
            {d.other_valid.map((p) => <PlanCard key={p.id} plan={p} actorId={actorId} isTop={false} canDecide={canDecide} onRegenerate={() => generate.mutate(true)} />)}
          </div>
        </details>
      )}

      {d.invalid.length > 0 && (
        <details className="rounded-panel border border-line bg-surface p-4">
          <summary className="cursor-pointer text-sm font-medium text-ink">Rejected by the checker ({d.invalid.length})</summary>
          <ul className="mt-3 space-y-3">
            {d.invalid.map((p) => (
              <li key={p.id} className="rounded-control bg-canvas p-3">
                <div className="mb-1 text-xs text-ink-muted capitalize">{p.outcome.replace('_', ' ')}</div>
                {p.violations.length > 0 ? <ViolationList violations={p.violations} /> : <p className="text-sm text-ink">{p.agent_rationale ?? 'The AI found no fix.'}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {d.decisions.length > 0 && (
        <Panel title="Decisions so far">
          <ul className="space-y-2 text-sm text-ink">
            {d.decisions.map((x) => (
              <li key={x.id} className="flex items-start gap-2">
                <FileText size={14} className="mt-0.5 shrink-0 text-ink-muted" aria-hidden="true" />
                <span>
                  <span className="font-medium">{x.actor_name ?? 'Someone'}</span>{' '}
                  {x.decision === 'approve' ? `approved plan ${x.plan_id}` : x.decision === 'reject_plan' ? `rejected plan ${x.plan_id}` : x.decision === 'reject_alert' ? 'rejected the alert' : 'dismissed the alert'}
                  {x.reason_code && ` (${REJECT_REASONS[x.reason_code] ?? x.reason_code})`}
                  {x.reason_text && `: ${x.reason_text}`}
                  <span className="text-ink-muted"> · {fmtDate(x.sim_time)}</span>
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <ConfirmDialog
        open={rejecting}
        onOpenChange={setRejecting}
        title="Reject this alert?"
        description="All plans for this alert are rejected. The alert stays closed unless the course gets worse."
        confirmLabel="Reject alert"
        tone="danger"
        busy={rejectAll.isPending}
        onConfirm={() => rejectAll.mutate()}
      >
        <div className="space-y-3">
          <SelectField label="Reason" value={reasonCode} onChange={(e) => setReasonCode(e.target.value)}>
            {Object.entries(REJECT_REASONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </SelectField>
          <TextAreaField label="Details" required={reasonCode === 'other'} value={reasonText} onChange={(e) => setReasonText(e.target.value)} />
          {rejectAll.error && <p role="alert" className="text-sm text-risk-high">{rejectAll.error.message}</p>}
        </div>
      </ConfirmDialog>
    </div>
  )
}

export default function Alerts() {
  const [params, setParams] = useSearchParams()
  const [tabKey, setTabKey] = useState('ready')
  const [actor, setActor] = useActor()
  const trainers = useAllTrainers()
  const alerts = useQuery({ queryKey: ['alerts', 'all'], queryFn: () => listAlerts(), refetchInterval: 5000 })
  const sim = useQuery({ queryKey: ['sim', 'state'], queryFn: getSimState })

  const all = useMemo(
    () => [...(alerts.data ?? [])].sort((a, b) => (RISK_RANK[a.risk_level] ?? 2) - (RISK_RANK[b.risk_level] ?? 2) || (b.updated_at ?? b.created_at).localeCompare(a.updated_at ?? a.created_at)),
    [alerts.data],
  )
  const tab = TABS.find((t) => t.key === tabKey) ?? TABS[0]
  const inTab = (t: (typeof TABS)[number]) => (t.statuses.length === 0 ? all : all.filter((a) => t.statuses.includes(a.status)))
  const list = inTab(tab)

  const selectedId = Number(params.get('alert')) || null
  const selected = (alerts.data ?? []).find((a) => a.id === selectedId) ?? null

  // A link from elsewhere (?alert=5) may point at an alert that is not in the current tab: switch to its tab.
  useEffect(() => {
    if (selected && !inTab(tab).some((a) => a.id === selected.id)) {
      const home = TABS.find((t) => t.statuses.includes(selected.status))
      if (home) setTabKey(home.key)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, selected?.status])

  useEffect(() => {
    if (selectedId === null && list.length > 0 && window.matchMedia('(min-width: 1024px)').matches) {
      setParams({ alert: String(list[0].id) }, { replace: true })
    }
  }, [selectedId, list, setParams])

  const readyCount = all.filter((a) => a.status === 'plans_ready').length

  return (
    <div className="space-y-6">
      <PageHeader
        title="Alerts"
        lead={alerts.isPending ? undefined : readyCount > 0 ? `${readyCount} alert${readyCount === 1 ? ' has' : 's have'} plans waiting for your decision.` : 'No plans are waiting for a decision right now.'}
        actions={
          <div className="w-56">
            <SelectField label="Act as trainer" value={actor ?? ''} onChange={(e) => setActor(e.target.value ? Number(e.target.value) : null)}>
              <option value="">Choose…</option>
              {(trainers.data?.items ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </SelectField>
          </div>
        }
      />
      {alerts.isError && <ErrorState title="Alerts could not load" message={alerts.error.message} onRetry={() => void alerts.refetch()} />}
      {alerts.isPending && <Skeleton className="h-64 w-full" />}

      {alerts.data && (
        <div className="grid gap-6 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className={cn('space-y-3', selected && 'hidden lg:block')}>
            <div className="flex flex-wrap gap-2" role="group" aria-label="Which alerts to show">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  aria-pressed={tabKey === t.key}
                  onClick={() => setTabKey(t.key)}
                  className={cn('rounded-full border px-3 py-1 text-sm', tabKey === t.key ? 'border-primary bg-primary text-white' : 'border-line bg-surface hover:bg-canvas')}
                >
                  {t.label} <span className="opacity-70">{inTab(t).length}</span>
                </button>
              ))}
            </div>
            {list.length === 0 ? (
              <EmptyState title="Nothing here">Alerts open when a course is likely to miss its yearly target, and close by themselves when it recovers.</EmptyState>
            ) : (
              <ul className="space-y-2">
                {list.map((a) => <AlertRow key={a.id} alert={a} selected={a.id === selected?.id} onSelect={() => setParams({ alert: String(a.id) })} />)}
              </ul>
            )}
          </div>

          <div className={cn('min-w-0', !selected && 'hidden lg:block')}>
            {selected ? (
              <>
                <Button variant="ghost" size="sm" className="mb-3 lg:hidden" onClick={() => setParams({})}>
                  <ArrowLeft size={14} aria-hidden="true" /> All alerts
                </Button>
                <Detail key={selected.id} alertId={selected.id} simNow={sim.data?.current_time} actorId={actor} />
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
