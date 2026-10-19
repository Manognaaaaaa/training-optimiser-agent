import { cn } from '../../lib/cn'
import { useLiveStatus, type LiveStatus as Status } from '../../live/liveStore'
import { Hint } from '../ui'

const LOOK: Record<Status, { label: string; dot: string; hint: string }> = {
  live: { label: 'Live', dot: 'bg-risk-ok', hint: 'Connected: this page updates by itself when data changes.' },
  reconnecting: { label: 'Reconnecting', dot: 'bg-risk-medium', hint: 'The live connection dropped. Trying again…' },
  offline: { label: 'Offline: polling', dot: 'bg-risk-unknown', hint: 'No live connection. Checking for changes every 15 seconds.' },
}

/** Top-bar pill showing whether live updates are flowing. */
export function LiveStatus() {
  const status = useLiveStatus()
  const look = LOOK[status]
  return (
    <Hint text={look.hint}>
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-surface px-2.5 py-1 text-xs font-medium text-ink" role="status">
        <span className={cn('h-2 w-2 rounded-full', look.dot)} aria-hidden="true" />
        {look.label}
      </span>
    </Hint>
  )
}
