import { AlertTriangle, CalendarCheck, CalendarX, Clock3, ExternalLink, Pencil } from 'lucide-react'
import { SYNC_LABEL, type CalendarInfo, type SyncStatus } from '../api/calendarSync'
import { cn } from '../lib/cn'
import { Hint } from './ui'

const ICON: Record<SyncStatus, { Icon: typeof Clock3; tone: string }> = {
  none: { Icon: CalendarX, tone: 'text-ink-muted' },
  pending: { Icon: Clock3, tone: 'text-risk-medium' },
  synced: { Icon: CalendarCheck, tone: 'text-risk-ok' },
  failed: { Icon: AlertTriangle, tone: 'text-risk-high' },
  cancelled: { Icon: CalendarX, tone: 'text-ink-muted' },
  drift: { Icon: Pencil, tone: 'text-risk-medium' },
}

/** A small icon with a tooltip for the Google Calendar sync state of one session. */
export function SyncIcon({ info, size = 12, className }: { info?: CalendarInfo | null; size?: number; className?: string }) {
  const status = info?.status ?? 'none'
  if (status === 'none') return null
  const { Icon, tone } = ICON[status]
  return (
    <Hint text={SYNC_LABEL[status]}>
      <span role="img" aria-label={SYNC_LABEL[status]} className={cn('inline-flex', tone, className)}>
        <Icon size={size} aria-hidden="true" />
      </span>
    </Hint>
  )
}

/** Status text plus an "Open in Google Calendar" link when the event exists. */
export function SyncLine({ info }: { info?: CalendarInfo | null }) {
  const status = info?.status ?? 'none'
  return (
    <span className="inline-flex flex-wrap items-center gap-2 text-sm">
      <SyncIcon info={info} size={14} />
      <span>{SYNC_LABEL[status]}</span>
      {info?.html_link && status !== 'cancelled' && (
        <a href={info.html_link} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
          Open in Google Calendar <ExternalLink size={12} aria-hidden="true" />
        </a>
      )}
    </span>
  )
}
