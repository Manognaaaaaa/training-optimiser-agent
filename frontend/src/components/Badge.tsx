import type { ReactNode } from 'react'
import type { FillBand } from '../api/sessions'
import type { RiskLevel } from '../api/tracking'
import { RiskPill, Tag } from './ui'
import { cn } from '../lib/cn'

type Tone = 'neutral' | 'good' | 'warn' | 'bad' | 'brand'

const TONES: Record<Tone, string> = {
  neutral: 'bg-risk-unknown-bg text-ink-muted',
  good: 'bg-risk-ok-bg text-risk-ok',
  warn: 'bg-risk-medium-bg text-risk-medium',
  bad: 'bg-risk-high-bg text-risk-high',
  brand: 'bg-primary-soft text-primary',
}

/** Small pill. Colour is only used where it means something (good / warn / bad); everything else is neutral. */
export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={cn('inline-block whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium', TONES[tone])}>{children}</span>
}

const STATUS_TONE: Record<string, Tone> = {
  scheduled: 'brand',
  completed: 'good',
  cancelled: 'neutral',
  booked: 'brand',
  attended: 'good',
  no_show: 'bad',
}

const STATUS_LABEL: Record<string, string> = {
  scheduled: 'Scheduled', completed: 'Completed', cancelled: 'Cancelled', booked: 'Booked', attended: 'Attended', no_show: 'No-show',
}

/** Session or enrollment status. */
export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? 'neutral'}>{STATUS_LABEL[status] ?? status}</Badge>
}

/** Shift is a plain fact, not a risk, so it is a neutral tag. */
export function ShiftBadge({ shift }: { shift: string }) {
  return <Tag>{shift.charAt(0).toUpperCase() + shift.slice(1)}</Tag>
}

const BAND_TONE: Record<FillBand, Tone> = { low: 'warn', medium: 'brand', high: 'good', cancelled: 'neutral' }
const BAND_LABEL: Record<FillBand, string> = { low: 'Low fill', medium: 'Medium fill', high: 'High fill', cancelled: 'Cancelled' }

/** Fill band with the percentage, e.g. "Low fill 33%". */
export function FillBadge({ band, rate }: { band: FillBand; rate: number | null }) {
  return (
    <Badge tone={BAND_TONE[band]}>
      {BAND_LABEL[band]}
      {rate !== null && ` ${Math.round(rate * 100)}%`}
    </Badge>
  )
}

export function ActiveBadge({ active }: { active: boolean }) {
  return <Badge tone={active ? 'good' : 'neutral'}>{active ? 'Active' : 'Inactive'}</Badge>
}

export const RiskBadge = ({ level }: { level: RiskLevel }) => <RiskPill level={level} />

export function MandatoryBadge({ mandatory }: { mandatory: boolean }) {
  return <Tag>{mandatory ? 'Mandatory' : 'Optional'}</Tag>
}
