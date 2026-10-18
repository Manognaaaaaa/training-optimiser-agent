/** Chart colours and month helpers shared by the Recharts charts. Wording and number formats live in lib/format.ts. */

/** Series colours. Risk colours (red, amber, green) are reserved for risk pills; yellow is only the "today" marker. */
export const SERIES = {
  actual: '#0f5e6b',
  forecast: '#0f5e6b',
  band: '#0f5e6b',
  pace: '#5b6875',
  target: '#17212b',
  grid: '#dde2e8',
  axis: '#5b6875',
  event: '#17212b',
  today: '#f2b705',
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
export const monthName = (ms: number) => MONTHS[new Date(ms).getMonth()]

/** Millisecond timestamps of the 1st of every month of the year containing ``ms``. */
export function monthTicks(ms: number): number[] {
  const year = new Date(ms).getFullYear()
  return MONTHS.map((_, m) => new Date(year, m, 1).getTime())
}

/** 'Mar 14' for tooltips. */
export const dayLabel = (ms: number) => {
  const d = new Date(ms)
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`
}
