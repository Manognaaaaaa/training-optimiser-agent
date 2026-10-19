import { BadgeCheck, CircleAlert, CircleCheck, CircleHelp, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import type { RiskLevel } from '../../api/tracking'
import { cn } from '../../lib/cn'
import { RISK_LABEL, RISK_TONE, type RiskTone } from '../../lib/format'

const TONE_CLASS: Record<RiskTone, string> = {
  high: 'bg-risk-high-bg text-risk-high',
  medium: 'bg-risk-medium-bg text-risk-medium',
  ok: 'bg-risk-ok-bg text-risk-ok',
  unknown: 'bg-risk-unknown-bg text-risk-unknown',
}

const ICON: Record<RiskLevel, ReactNode> = {
  high: <TriangleAlert size={14} aria-hidden="true" />,
  medium: <CircleAlert size={14} aria-hidden="true" />,
  low: <CircleCheck size={14} aria-hidden="true" />,
  achieved: <BadgeCheck size={14} aria-hidden="true" />,
  insufficient_data: <CircleHelp size={14} aria-hidden="true" />,
}

/** Risk as an icon plus words, so colour is never the only signal. */
export function RiskPill({ level, className }: { level: RiskLevel; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium', TONE_CLASS[RISK_TONE[level]], className)}>
      {ICON[level]}
      {RISK_LABEL[level]}
    </span>
  )
}

/** Neutral small tag, e.g. Mandatory / Optional. */
export function Tag({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return <span title={title} className={cn('inline-block whitespace-nowrap rounded-full bg-risk-unknown-bg px-2 py-0.5 text-xs text-ink-muted', className)}>{children}</span>
}
