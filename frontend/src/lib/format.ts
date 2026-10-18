/**
 * Every number, date, percentage and label the UI shows goes through here, so wording stays the same everywhere.
 * Nothing in this file touches the network: it only turns backend values into plain-language text.
 */
import type { CourseRow, RiskLevel, ShortfallType } from '../api/tracking'

// --- Numbers and dates -------------------------------------------------------------------------
export const fmtNumber = (n: number): string => Math.round(n).toLocaleString('en-GB')

export const fmtPercent = (n: number, digits = 0): string => `${n.toFixed(digits)}%`

/** Backend datetimes are naive local times, so `new Date(value)` reads them as local time (see utils/datetime). */
const toDate = (value: string | Date): Date => (typeof value === 'string' ? new Date(value) : value)

/** '9 Mar 2026' */
export const fmtDate = (value: string | Date): string =>
  toDate(value).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })

/** 'Tue 9 Mar, 14:30' */
export const fmtDateTime = (value: string | Date): string => {
  const d = toDate(value)
  const day = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  return `${day}, ${time}`
}

// --- Chance of hitting the target ----------------------------------------------------------------
export type ChanceBucket = 'very_unlikely' | 'unlikely' | 'toss_up' | 'likely' | 'very_likely'

export const CHANCE_LABEL: Record<ChanceBucket, string> = {
  very_unlikely: 'Very unlikely',
  unlikely: 'Unlikely',
  toss_up: 'Toss-up',
  likely: 'Likely',
  very_likely: 'Very likely',
}

/** Buckets, on the rounded percentage: under 10 very unlikely, 10-39 unlikely, 40-60 toss-up, 61-89 likely, 90+ very likely. */
export function chanceBucket(p: number): ChanceBucket {
  const percent = Math.round(p * 100)
  if (percent < 10) return 'very_unlikely'
  if (percent < 40) return 'unlikely'
  if (percent <= 60) return 'toss_up'
  if (percent < 90) return 'likely'
  return 'very_likely'
}

/** The percentage never shows a misleading 0% or 100%. */
export function fmtProb(p: number): string {
  if (p < 0.01) return '<1%'
  if (p > 0.99) return '>99%'
  return `${Math.round(p * 100)}%`
}

/** 'Unlikely (14%)' */
export const fmtChance = (p: number): string => `${CHANCE_LABEL[chanceBucket(p)]} (${fmtProb(p)})`

// --- Progress against schedule --------------------------------------------------------------------
export type Tone = 'good' | 'bad' | 'neutral'

/** pace_gap is attended minus the target pace today. Positive = ahead. */
export function scheduleDelta(paceGap: number): { text: string; tone: Tone } {
  const rounded = Math.round(paceGap)
  if (rounded > 0) return { text: `${fmtNumber(rounded)} ahead`, tone: 'good' }
  if (rounded < 0) return { text: `${fmtNumber(-rounded)} behind`, tone: 'bad' }
  return { text: 'On pace', tone: 'neutral' }
}

/** How many completions short of the target the year-end projection is (never negative). */
export const shortBy = (c: Pick<CourseRow, 'target' | 'projected'>): number => Math.max(0, Math.round(c.target - c.projected))

/** 'Short by ~45 completions', or 'Not short' when the projection reaches the target. */
export function shortByText(c: Pick<CourseRow, 'target' | 'projected'>): string {
  const gap = shortBy(c)
  if (gap === 0) return 'Not short'
  return `Short by ~${fmtNumber(gap)} ${gap === 1 ? 'completion' : 'completions'}`
}

// --- Risk -----------------------------------------------------------------------------------------
export const RISK_LABEL: Record<RiskLevel, string> = {
  high: 'Will likely miss',
  medium: 'At risk',
  low: 'On track',
  achieved: 'Target met',
  insufficient_data: 'Not enough data',
}

export type RiskTone = 'high' | 'medium' | 'ok' | 'unknown'

export const RISK_TONE: Record<RiskLevel, RiskTone> = {
  high: 'high',
  medium: 'medium',
  low: 'ok',
  achieved: 'ok',
  insufficient_data: 'unknown',
}

/** Sort order for the course list: worst first. */
export const RISK_ORDER: Record<RiskLevel, number> = { high: 0, medium: 1, insufficient_data: 2, low: 3, achieved: 4 }

export const needsAttention = (level: RiskLevel): boolean => level === 'high' || level === 'medium'

export const PROBLEM_LABEL: Record<ShortfallType, string> = {
  capacity_gap: 'Not enough seats',
  attendance_gap: 'Low turnout',
  pool_gap: 'Not enough eligible drivers',
  none: 'No problem found',
}

/** One sentence that says why, built from the numbers the backend already sends. Falls back to the backend's own text. */
export function whySentence(c: CourseRow): string {
  const stillNeeded = Math.max(0, c.target - c.attended)
  switch (c.shortfall_type) {
    case 'capacity_gap':
      return `Needs about ${fmtNumber(c.seats_needed)} seats to fill, but only ${fmtNumber(c.remaining_seats)} are left.`
    case 'attendance_gap':
      return `There are enough seats (${fmtNumber(c.remaining_seats)} left), but only ${Math.round(c.p_show * 100)}% of booked drivers show up, so they will not all turn into completions.`
    case 'pool_gap':
      return `Only ${fmtNumber(c.eligible_pool)} eligible drivers are left, but ${fmtNumber(stillNeeded)} more completions are needed.`
    default:
      return c.reasons[0]?.message ?? (c.risk_level === 'achieved' ? 'The target has been reached.' : 'No shortfall expected at the current pace.')
  }
}

/** The lead sentence on the dashboard. */
export function dashboardSummary(courses: Pick<CourseRow, 'risk_level'>[], openAlerts: number, year = 2026): string {
  const high = courses.filter((c) => c.risk_level === 'high').length
  const medium = courses.filter((c) => c.risk_level === 'medium').length
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
  const alerts = openAlerts === 0 ? 'No alerts need review.' : `${plural(openAlerts, 'alert needs', 'alerts need')} review.`

  if (high === 0 && medium === 0) return `All courses are on track for their ${year} target. ${alerts}`
  if (high === 0) return `${plural(medium, 'course is', 'courses are')} at risk of missing the ${year} target. ${alerts}`
  const head = `${plural(high, 'course', 'courses')} will likely miss their ${year} target`
  const tail = medium > 0 ? ` and ${medium} more ${medium === 1 ? 'is' : 'are'} at risk` : ''
  return `${head}${tail}. ${alerts}`
}

const CAUSE: Record<ShortfallType, string> = {
  capacity_gap: "there aren't enough seats left",
  attendance_gap: 'too few of the booked drivers turn up',
  pool_gap: "there aren't enough eligible drivers left",
  none: '',
}

/** The sentence under a course's title. */
export function courseSummary(c: CourseRow): string {
  if (c.risk_level === 'achieved') return `Target met: ${fmtNumber(c.attended)} of ${fmtNumber(c.target)} completed.`
  if (c.risk_level === 'insufficient_data') return 'Not enough weeks of data yet to forecast this course.'
  const head = `Expected to finish at ${fmtNumber(c.projected)} of ${fmtNumber(c.target)}.`
  const gap = shortBy(c)
  if (gap === 0) return `${head} On track.`
  const cause = CAUSE[c.shortfall_type]
  return `${head} Short by about ${fmtNumber(gap)}${cause ? `, mostly because ${cause}` : ''}.`
}
