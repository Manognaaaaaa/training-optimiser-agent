import { describe, expect, it } from 'vitest'
import { keysFor, toastFor } from './liveMap'

describe('keysFor', () => {
  it('refreshes dashboards, calendar and clock when the sim moves', () => {
    const keys = keysFor('sim.advanced', {})
    expect(keys).toEqual(expect.arrayContaining([['tracking'], ['calendar'], ['sim']]))
  })
  it('targets the alert detail by id', () => {
    expect(keysFor('alert.updated', { alert_id: 7 })).toContainEqual(['alert', 7])
    expect(keysFor('alert.created', {})).toContainEqual(['alert'])
  })
  it('maps master data to its own lists', () => {
    expect(keysFor('master_data.changed', { entity: 'trainers' })).toContainEqual(['all-trainers'])
  })
  it('invalidates everything on resync and nothing for progress or heartbeats', () => {
    expect(keysFor('resync', {})).toBe('all')
    expect(keysFor('planner_run.progress', {})).toEqual([])
    expect(keysFor('heartbeat', {})).toEqual([])
  })
})

describe('toastFor', () => {
  it('only toasts for high-risk new alerts', () => {
    expect(toastFor('alert.created', { risk_level: 'high', course_name: 'Defensive Driving' })?.text).toContain('Defensive Driving')
    expect(toastFor('alert.created', { risk_level: 'medium' })).toBeNull()
  })
  it('toasts plans ready and approvals', () => {
    expect(toastFor('alert.updated', { status: 'plans_ready', course_name: 'X' })).not.toBeNull()
    expect(toastFor('plan.decided', { decision: 'approve', plan_id: 3 })?.level).toBe('success')
    expect(toastFor('plan.decided', { decision: 'reject_plan' })).toBeNull()
  })
})
