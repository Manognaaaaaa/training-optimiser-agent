import { describe, expect, it } from 'vitest'
import type { CourseRow } from '../api/tracking'
import {
  chanceBucket, dashboardSummary, fmtChance, fmtDate, fmtNumber, fmtPercent, fmtProb, needsAttention, scheduleDelta, shortBy, shortByText,
  whySentence, RISK_LABEL, courseSummary,
} from './format'

const course = (over: Partial<CourseRow> = {}): CourseRow =>
  ({
    course_id: 1, code: 'DEF', name: 'Defensive driving', is_mandatory: true, target: 240, attended: 44, target_pace: 39, pace_gap: 5,
    p_show: 0.82, p_fill: 0.7, remaining_sessions: 10, remaining_seats: 225, eligible_pool: 150, projected: 195, low: 167, high: 223,
    p_hit: 0.02, naive_projection: 190, linear_projection: 200, risk_level: 'high', shortfall_type: 'capacity_gap', seats_needed: 257,
    weeks_observed: 20, reasons: [{ code: 'x', message: 'Backend reason', value: 1, benchmark: 1, impact: 1, group: null, by: null }],
    open_alert_id: null, ...over,
  }) as CourseRow

describe('numbers and dates', () => {
  it('formats numbers with thousands separators and rounds', () => {
    expect(fmtNumber(1190)).toBe('1,190')
    expect(fmtNumber(44.6)).toBe('45')
  })
  it('formats percentages', () => {
    expect(fmtPercent(18.64, 1)).toBe('18.6%')
    expect(fmtPercent(16)).toBe('16%')
  })
  it('formats a date as day, short month, year', () => {
    expect(fmtDate(new Date(2026, 2, 9))).toBe('9 Mar 2026')
  })
})

describe('chance of hitting the target', () => {
  it('uses the agreed buckets', () => {
    expect(chanceBucket(0.004)).toBe('very_unlikely')
    expect(chanceBucket(0.09)).toBe('very_unlikely')
    expect(chanceBucket(0.1)).toBe('unlikely')
    expect(chanceBucket(0.39)).toBe('unlikely')
    expect(chanceBucket(0.4)).toBe('toss_up')
    expect(chanceBucket(0.44)).toBe('toss_up')
    expect(chanceBucket(0.6)).toBe('toss_up')
    expect(chanceBucket(0.61)).toBe('likely')
    expect(chanceBucket(0.89)).toBe('likely')
    expect(chanceBucket(0.9)).toBe('very_likely')
    expect(chanceBucket(0.98)).toBe('very_likely')
  })
  it('never shows a misleading 0% or 100%', () => {
    expect(fmtProb(0)).toBe('<1%')
    expect(fmtProb(1)).toBe('>99%')
    expect(fmtProb(0.14)).toBe('14%')
  })
  it('combines label and percentage', () => {
    expect(fmtChance(0.004)).toBe('Very unlikely (<1%)')
    expect(fmtChance(0.14)).toBe('Unlikely (14%)')
    expect(fmtChance(0.44)).toBe('Toss-up (44%)')
    expect(fmtChance(0.98)).toBe('Very likely (98%)')
  })
})

describe('schedule delta', () => {
  it('says ahead, behind or on pace', () => {
    expect(scheduleDelta(5)).toEqual({ text: '5 ahead', tone: 'good' })
    expect(scheduleDelta(-2.4)).toEqual({ text: '2 behind', tone: 'bad' })
    expect(scheduleDelta(0.3)).toEqual({ text: 'On pace', tone: 'neutral' })
    expect(scheduleDelta(-0.4)).toEqual({ text: 'On pace', tone: 'neutral' })
  })
})

describe('shortfall', () => {
  it('computes the gap and never goes negative', () => {
    expect(shortBy({ target: 240, projected: 195 })).toBe(45)
    expect(shortBy({ target: 240, projected: 260 })).toBe(0)
  })
  it('words it', () => {
    expect(shortByText({ target: 240, projected: 195 })).toBe('Short by ~45 completions')
    expect(shortByText({ target: 240, projected: 239 })).toBe('Short by ~1 completion')
    expect(shortByText({ target: 240, projected: 250 })).toBe('Not short')
  })
})

describe('risk', () => {
  it('has plain labels', () => {
    expect(RISK_LABEL.high).toBe('Will likely miss')
    expect(RISK_LABEL.medium).toBe('At risk')
    expect(RISK_LABEL.low).toBe('On track')
    expect(RISK_LABEL.achieved).toBe('Target met')
    expect(RISK_LABEL.insufficient_data).toBe('Not enough data')
  })
  it('flags high and medium as needing attention', () => {
    expect(needsAttention('high')).toBe(true)
    expect(needsAttention('medium')).toBe(true)
    expect(needsAttention('low')).toBe(false)
  })
})

describe('why sentence', () => {
  it('explains a seat shortage with numbers', () => {
    expect(whySentence(course())).toBe('Needs about 257 seats to fill, but only 225 are left.')
  })
  it('explains low turnout', () => {
    expect(whySentence(course({ shortfall_type: 'attendance_gap' }))).toContain('only 82% of booked drivers show up')
  })
  it('explains a small pool', () => {
    expect(whySentence(course({ shortfall_type: 'pool_gap', eligible_pool: 10 }))).toBe('Only 10 eligible drivers are left, but 196 more completions are needed.')
  })
  it('falls back to the backend text', () => {
    expect(whySentence(course({ shortfall_type: 'none', risk_level: 'low' }))).toBe('Backend reason')
  })
})

describe('dashboard summary sentence', () => {
  const rows = (...levels: CourseRow['risk_level'][]) => levels.map((risk_level) => ({ risk_level }))
  it('mentions high and medium courses and open alerts', () => {
    expect(dashboardSummary(rows('high', 'high', 'high', 'high', 'high', 'medium', 'medium', 'low'), 7)).toBe(
      '5 courses will likely miss their 2026 target and 2 more are at risk. 7 alerts need review.',
    )
  })
  it('uses singular forms', () => {
    expect(dashboardSummary(rows('high', 'medium'), 1)).toBe('1 course will likely miss their 2026 target and 1 more is at risk. 1 alert needs review.')
  })
  it('handles only at-risk courses', () => {
    expect(dashboardSummary(rows('medium', 'medium'), 0)).toBe('2 courses are at risk of missing the 2026 target. No alerts need review.')
  })
  it('says so when everything is on track', () => {
    expect(dashboardSummary(rows('low', 'achieved'), 0)).toBe('All courses are on track for their 2026 target. No alerts need review.')
  })
})

describe('course summary sentence', () => {
  it('says what is expected and the main cause', () => {
    expect(courseSummary(course())).toBe("Expected to finish at 195 of 240. Short by about 45, mostly because there aren't enough seats left.")
  })
  it('handles courses that are fine', () => {
    expect(courseSummary(course({ projected: 250, risk_level: 'low', shortfall_type: 'none' }))).toBe('Expected to finish at 250 of 240. On track.')
    expect(courseSummary(course({ risk_level: 'achieved' }))).toBe('Target met: 44 of 240 completed.')
    expect(courseSummary(course({ risk_level: 'insufficient_data' }))).toBe('Not enough weeks of data yet to forecast this course.')
  })
})
