import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { cn } from '../../lib/cn'

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden="true" className={cn('animate-[skeleton_1.4s_ease-in-out_infinite] rounded-control bg-line/70', className)} />
}

/** Page title block. `lead` is the one sentence that tells the reader what to look at first. */
export function PageHeader({
  title, lead, actions, breadcrumb,
}: {
  title: string
  lead?: ReactNode
  actions?: ReactNode
  breadcrumb?: { label: string; to?: string }[]
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        {breadcrumb && (
          <nav aria-label="Breadcrumb" className="mb-1 flex items-center gap-1 text-xs text-ink-muted">
            {breadcrumb.map((b, i) => (
              <span key={b.label} className="flex items-center gap-1">
                {i > 0 && <ChevronRight size={12} aria-hidden="true" />}
                {b.to ? <Link to={b.to} className="hover:text-ink hover:underline">{b.label}</Link> : <span>{b.label}</span>}
              </span>
            ))}
          </nav>
        )}
        <h1 className="font-heading text-3xl font-semibold leading-tight text-ink">{title}</h1>
        {lead && <div className="mt-1 max-w-3xl text-base text-ink">{lead}</div>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}

/** A bordered block. No shadow: only dialogs and drawers float. */
export function Panel({
  title, actions, children, className, padded = true,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  padded?: boolean
}) {
  return (
    <section className={cn('rounded-panel border border-line bg-surface', className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          {title && <h2 className="font-heading text-xl font-semibold text-ink">{title}</h2>}
          {actions}
        </header>
      )}
      <div className={padded ? 'p-4' : ''}>{children}</div>
    </section>
  )
}

/** One headline number. The label is plain sentence case and the value is the biggest thing in the card. */
export function StatCard({
  label, value, note, loading, info,
}: {
  label: ReactNode
  value: ReactNode
  note?: ReactNode
  loading?: boolean
  info?: ReactNode
}) {
  return (
    <div className="rounded-panel border border-line bg-surface p-4">
      <div className="flex items-center gap-1.5 text-sm text-ink-muted">
        {label}
        {info}
      </div>
      {loading ? (
        <>
          <Skeleton className="mt-2 h-8 w-24" />
          <Skeleton className="mt-2 h-3 w-36" />
        </>
      ) : (
        <>
          <div className="mt-1 font-heading text-3xl font-semibold leading-tight text-ink">{value}</div>
          {note && <div className="mt-1 text-xs text-ink-muted">{note}</div>}
        </>
      )}
    </div>
  )
}
