import { CircleAlert, Inbox } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from './Button'

/** Nothing to show yet. Say what will appear here and, if it is a later phase, which one. */
export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-panel border border-dashed border-line bg-surface px-6 py-10 text-center">
      <Inbox className="mx-auto text-ink-muted" size={24} aria-hidden="true" />
      <div className="mt-2 text-base font-medium text-ink">{title}</div>
      {children && <p className="mx-auto mt-1 max-w-md text-sm text-ink-muted">{children}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/** Says what failed and offers a retry. */
export function ErrorState({ title = 'Something went wrong', message, onRetry }: { title?: string; message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start gap-3 rounded-panel border border-risk-high/30 bg-risk-high-bg px-4 py-3">
      <CircleAlert className="mt-0.5 shrink-0 text-risk-high" size={18} aria-hidden="true" />
      <div className="flex-1">
        <div className="text-sm font-medium text-risk-high">{title}</div>
        <div className="text-sm text-ink">{message}</div>
      </div>
      {onRetry && (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  )
}
