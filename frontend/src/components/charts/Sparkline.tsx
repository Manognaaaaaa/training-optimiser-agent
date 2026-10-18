import { CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { SeriesPoint } from '../../api/tracking'
import { parseApi } from '../../utils/datetime'
import { dayLabel, monthName, monthTicks } from '../../utils/tracking'

interface Row {
  t: number
  actual: number | null
  pace: number
  forecast: number | null
}

const COLORS = { actual: '#0f5e6b', pace: '#5b6875', grid: '#dde2e8', today: '#f2b705' }

/** Tiny chart for one course: how far along it is (solid), where it should be (dashed), the forecast (dotted), and today (yellow). */
export function Sparkline({ series, target }: { series: SeriesPoint[]; target: number }) {
  const share = (v: number | null) => (v === null || target === 0 ? null : (100 * v) / target)
  const data: Row[] = series.map((p) => ({
    t: parseApi(p.date).getTime(), actual: share(p.actual), pace: share(p.target_pace) ?? 0, forecast: share(p.forecast_mean),
  }))
  if (data.length === 0) return null
  const today = parseApi((series.find((p) => p.is_now) ?? series[0]).date).getTime()
  const ticks = monthTicks(data[0].t).filter((_, i) => i % 3 === 0)
  const text = (v: number | null) => (v === null ? '–' : `${Math.round(v)}% of target`)

  return (
    <div className="h-28 w-full" role="img" aria-label="Progress so far compared with the target pace and the forecast for the rest of the year">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 4, right: 6, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={COLORS.grid} vertical={false} />
          <XAxis
            dataKey="t" type="number" scale="time" domain={[data[0].t, data[data.length - 1].t]} ticks={ticks}
            tickFormatter={monthName} tick={{ fontSize: 11, fill: COLORS.pace }} tickLine={false}
          />
          <YAxis domain={[0, 120]} ticks={[0, 100]} tickFormatter={(v: number) => `${v}%`} tick={{ fontSize: 11, fill: COLORS.pace }} tickLine={false} axisLine={false} width={44} />
          <Tooltip
            cursor={{ stroke: COLORS.pace, strokeDasharray: '3 3' }}
            content={(props) => {
              const row = props.active ? (props.payload?.[0]?.payload as Row | undefined) : undefined
              if (!row) return null
              return (
                <div className="rounded-control border border-line bg-surface px-2 py-1.5 text-xs text-ink">
                  <div className="font-medium">{dayLabel(row.t)}</div>
                  {row.actual !== null && <div>Done: {text(row.actual)}</div>}
                  <div>Should be: {text(row.pace)}</div>
                  {row.forecast !== null && <div>Forecast: {text(row.forecast)}</div>}
                </div>
              )
            }}
          />
          <ReferenceLine x={today} stroke={COLORS.today} strokeWidth={2} />
          <Line dataKey="pace" stroke={COLORS.pace} strokeWidth={1.25} strokeDasharray="5 3" dot={false} isAnimationActive={false} />
          <Line dataKey="forecast" stroke={COLORS.actual} strokeWidth={1.75} strokeDasharray="2 3" dot={false} isAnimationActive={false} />
          <Line dataKey="actual" stroke={COLORS.actual} strokeWidth={2} dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
