import { cn } from '../../lib/cn'

/**
 * value out of max, with an optional marker for where the value should be by now (the "target pace").
 * It has role="progressbar" and a text label, so screen readers get the same facts as the picture.
 */
export function ProgressBar({
  value, max, pace, label, className, barClassName,
}: {
  value: number
  max: number
  pace?: number
  label: string
  className?: string
  barClassName?: string
}) {
  const share = (n: number) => Math.max(0, Math.min(100, (100 * n) / Math.max(1, max)))
  const paceText = pace === undefined ? '' : `, should be at ${Math.round(pace)} by today`
  return (
    <div
      role="progressbar"
      aria-label={`${label}: ${Math.round(value)} of ${Math.round(max)}${paceText}`}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={Math.min(value, max)}
      className={cn('relative h-2 rounded-full bg-line', className)}
    >
      <div className={cn('h-2 rounded-full bg-primary', barClassName)} style={{ width: `${share(value)}%` }} />
      {pace !== undefined && (
        <div
          aria-hidden="true"
          title={`Should be at ${Math.round(pace)} by today`}
          className="absolute -top-1 h-4 w-0.5 rounded bg-ink"
          style={{ left: `calc(${share(pace)}% - 1px)` }}
        />
      )}
    </div>
  )
}
