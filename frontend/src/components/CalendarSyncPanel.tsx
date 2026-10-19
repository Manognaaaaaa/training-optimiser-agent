import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import {
  backfill, getSyncSummary, reconcile, resetCalendar, retryAllFailed, retryRow, type ReconcileReport,
} from '../api/calendarSync'
import { fmtDateTime } from '../lib/format'
import { Button, ConfirmDialog, ErrorState, Panel, Skeleton, Tag, TextField } from './ui'

const COUNT_LABELS = [
  ['pending', 'Waiting'], ['in_progress', 'Sending'], ['done', 'Done'], ['failed', 'Failed'], ['skipped', 'Merged'],
] as const

const plusDays = (from: string, days: number) => {
  const d = new Date(from)
  d.setDate(d.getDate() + days)
  return d.toISOString().slice(0, 10)
}

/** Google Calendar sync: status, failed rows, backfill, reconcile (drift report) and reset. Live: refetches on sync events. */
export function CalendarSyncPanel({ simNow }: { simNow: string }) {
  const queryClient = useQueryClient()
  const summary = useQuery({ queryKey: ['calendar-sync', 'summary'], queryFn: getSyncSummary })
  const [from, setFrom] = useState(simNow.slice(0, 10))
  const [to, setTo] = useState(plusDays(simNow, 7))
  const [report, setReport] = useState<ReconcileReport | null>(null)
  const [resetOpen, setResetOpen] = useState(false)
  const [typed, setTyped] = useState('')

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['calendar-sync'] })
  const fail = (e: Error) => toast.error(e.message)

  const retry = useMutation({ mutationFn: retryRow, onSuccess: refresh, onError: fail })
  const retryAll = useMutation({
    mutationFn: retryAllFailed, onError: fail,
    onSuccess: (r) => { toast.success(`${r.requeued} failed change${r.requeued === 1 ? '' : 's'} queued again.`); refresh() },
  })
  const fill = useMutation({
    mutationFn: () => backfill(`${from}T00:00:00`, `${to}T00:00:00`), onError: fail,
    onSuccess: (r) => { toast.success(`${r.enqueued} session${r.enqueued === 1 ? '' : 's'} queued for Google Calendar.`); refresh() },
  })
  const check = useMutation({ mutationFn: () => reconcile(`${from}T00:00:00`, `${to}T00:00:00`), onError: fail, onSuccess: setReport })
  const wipe = useMutation({
    mutationFn: () => resetCalendar(typed), onError: fail,
    onSuccess: (r) => {
      toast.success(`Removed ${r.events_deleted} event${r.events_deleted === 1 ? '' : 's'} from the sandbox calendar.`)
      setResetOpen(false)
      setTyped('')
      setReport(null)
      refresh()
    },
  })

  if (summary.isPending) return <Skeleton className="h-40 w-full" />
  if (summary.isError) return <ErrorState title="Calendar sync status could not load" message={summary.error.message} onRetry={() => void summary.refetch()} />
  const s = summary.data
  const failed = s.recent.filter((r) => r.status === 'failed')

  return (
    <Panel title="Google Calendar sync">
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Tag>{s.provider === 'google' ? 'Google Calendar' : 'Practice mode (no Google)'}</Tag>
          <span className="text-ink-muted">Calendar: <span className="font-mono">{s.calendar_id}</span></span>
          <span className="text-ink-muted">
            · {s.last_success_at ? `last sync ${fmtDateTime(s.last_success_at)} UTC` : 'nothing synced yet'}
          </span>
          {!s.enabled && <Tag>Sync is switched off</Tag>}
        </div>

        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {COUNT_LABELS.map(([key, label]) => (
            <div key={key} className="rounded-panel border border-line bg-surface p-3">
              <dt className="text-xs text-ink-muted">{label}</dt>
              <dd className="text-lg font-semibold">{s.counts[key] ?? 0}</dd>
            </div>
          ))}
        </dl>

        {s.counts.failed > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="font-heading text-lg font-semibold">Failed changes</h3>
              <Button size="sm" onClick={() => retryAll.mutate()} disabled={retryAll.isPending}>Retry all failed</Button>
            </div>
            <ul className="divide-y divide-line text-sm">
              {failed.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <span>
                    <span className="font-medium">Session {r.session_id}</span> · {r.attempts} attempt{r.attempts === 1 ? '' : 's'}
                    <span className="block text-risk-high">{r.last_error}</span>
                  </span>
                  <Button size="sm" onClick={() => retry.mutate(r.id)} disabled={retry.isPending}>Retry</Button>
                </li>
              ))}
              {failed.length === 0 && <li className="py-2 text-ink-muted">{s.counts.failed} failed. Use "Retry all failed" to queue them again.</li>}
            </ul>
          </div>
        )}

        <div className="space-y-3 border-t border-line pt-4">
          <h3 className="font-heading text-lg font-semibold">Send sessions or check for edits</h3>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-40"><TextField label="From" type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></div>
            <div className="w-40"><TextField label="To (up to 8 weeks)" type="date" value={to} onChange={(e) => setTo(e.target.value)} /></div>
            <Button variant="primary" onClick={() => fill.mutate()} disabled={fill.isPending}>Send these sessions</Button>
            <Button onClick={() => check.mutate()} disabled={check.isPending}>Check for differences</Button>
          </div>
          {report && (
            <div className="space-y-1 rounded-panel bg-canvas p-3 text-sm" role="status">
              <div>
                <span className="font-medium">{report.in_sync}</span> in sync · <span className="font-medium">{report.missing_in_google.length}</span> missing in Google ·{' '}
                <span className="font-medium">{report.content_mismatch.length}</span> edited in Google · <span className="font-medium">{report.orphaned_in_google.length}</span> left over in Google
              </div>
              {report.content_mismatch.length > 0 && <div>Edited: sessions {report.content_mismatch.map((m) => m.session_id).join(', ')}. Send them again to overwrite the edits.</div>}
              {report.missing_in_google.length > 0 && <div>Missing: sessions {report.missing_in_google.map((m) => m.session_id).join(', ')}.</div>}
              {report.orphaned_in_google.length > 0 && <div>Left over: {report.orphaned_in_google.map((o) => o.summary ?? o.event_id).join(', ')}.</div>}
            </div>
          )}
        </div>

        <div className="border-t border-line pt-4">
          <Button variant="danger" onClick={() => setResetOpen(true)}>Reset sandbox calendar</Button>
          <p className="mt-1 text-xs text-ink-muted">Deletes every event this app created in the sandbox calendar. Other events are never touched.</p>
        </div>
      </div>

      <ConfirmDialog
        open={resetOpen}
        onOpenChange={(o) => { setResetOpen(o); if (!o) setTyped('') }}
        title="Reset the sandbox calendar?"
        description="Every event created by this app (for this environment) is deleted from Google Calendar and the links are forgotten. The database is not changed."
        confirmLabel="Delete the events"
        tone="danger"
        busy={wipe.isPending}
        onConfirm={() => { if (typed === 'DELETE') wipe.mutate() }}
      >
        <TextField label="Type DELETE to confirm" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
      </ConfirmDialog>
    </Panel>
  )
}
