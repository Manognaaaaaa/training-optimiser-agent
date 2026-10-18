import {
  Area, CartesianGrid, ComposedChart, Legend, Line, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { CourseEvent, SeriesPoint } from '../../api/tracking'
import { parseApi } from '../../utils/datetime'
import { SERIES, dayLabel, monthName, monthTicks } from '../../utils/tracking'
import ChartCard, { TooltipBox, type TipProps } from './ChartCard'

interface Row {
  t: number
  actual: number | null
  pace: number
  forecast: number | null
  band: [number, number] | null
}

const round = (n: number | null) => (n === null ? '–' : String(Math.round(n)))

/** Main chart: cumulative actual (solid), target pace (dashed), forecast (dotted) with a 90% band, target and "Sim now" lines. */
export default function CourseChart({ series, target, events }: { series: SeriesPoint[]; target: number; events: CourseEvent[] }) {
  const data: Row[] = series.map((p) => ({
    t: parseApi(p.date).getTime(),
    actual: p.actual,
    pace: p.target_pace,
    forecast: p.forecast_mean,
    band: p.forecast_low !== null && p.forecast_high !== null ? [p.forecast_low, p.forecast_high] : null,
  }))
  const now = series.find((p) => p.is_now)
  const nowT = now ? parseApi(now.date).getTime() : null
  const top = Math.max(target, ...series.map((p) => p.forecast_high ?? 0), ...series.map((p) => p.actual ?? 0))
  const yMax = Math.ceil((top * 1.08) / 10) * 10
  const ticks = monthTicks(data[0].t)

  /** y position of an event marker: the target pace at that date (the pace line spans the whole year). */
  const paceAt = (t: number) => data.reduce((best, r) => (Math.abs(r.t - t) < Math.abs(best.t - t) ? r : best), data[0]).pace

  return (
    <ChartCard
      title="Progress through the year"
      note="Solid line: completed so far. Dashed: where the course should be. Dotted and shaded: forecast and likely range. Yellow line: today."
      table={{
        head: ['Date', 'Done so far', 'Should be', 'Forecast', 'Likely low', 'Likely high'],
        rows: series.map((p) => [
          dayLabel(parseApi(p.date).getTime()), round(p.actual), round(p.target_pace), round(p.forecast_mean),
          round(p.forecast_low), round(p.forecast_high),
        ]),
      }}
    >
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 16, right: 24, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={SERIES.grid} vertical={false} />
            <XAxis
              dataKey="t" type="number" scale="time" domain={[ticks[0], data[data.length - 1].t]} ticks={ticks}
              tickFormatter={monthName} tick={{ fontSize: 11, fill: SERIES.axis }} tickLine={false}
            />
            <YAxis label={{ value: 'Completions', angle: -90, position: 'insideLeft', fontSize: 11, fill: SERIES.axis }} domain={[0, yMax]} tick={{ fontSize: 11, fill: SERIES.axis }} tickLine={false} axisLine={false} width={48} />
            <Tooltip
              cursor={{ stroke: SERIES.axis, strokeDasharray: '3 3' }}
              content={(props: TipProps) => {
                const row = props.active ? (props.payload?.[0]?.payload as Row | undefined) : undefined
                if (!row) return null
                const rows: [string, string][] = []
                if (row.actual !== null) rows.push(['Done so far', round(row.actual)])
                rows.push(['Should be', round(row.pace)])
                if (row.forecast !== null) rows.push(['Forecast', round(row.forecast)])
                if (row.band) rows.push(['Likely range', `${round(row.band[0])} to ${round(row.band[1])}`])
                return <TooltipBox title={dayLabel(row.t)} rows={rows} />
              }}
            />
            <Legend verticalAlign="top" height={28} iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
            <Area dataKey="band" name="Likely range" stroke="none" fill={SERIES.band} fillOpacity={0.15} isAnimationActive={false} legendType="square" />
            <Line dataKey="pace" name="Should be" stroke={SERIES.pace} strokeWidth={1.5} strokeDasharray="6 4" dot={false} isAnimationActive={false} />
            <Line dataKey="forecast" name="Forecast" stroke={SERIES.forecast} strokeWidth={2} strokeDasharray="2 4" strokeLinecap="round" dot={false} isAnimationActive={false} />
            <Line dataKey="actual" name="Done so far" stroke={SERIES.actual} strokeWidth={2} dot={false} isAnimationActive={false} />
            <ReferenceLine
              y={target} stroke={SERIES.target} strokeWidth={1}
              label={{ value: `Target ${target}`, position: 'insideTopLeft', fontSize: 11, fill: SERIES.target }}
            />
            {nowT !== null && (
              <ReferenceLine x={nowT} stroke={SERIES.today} strokeWidth={2} label={{ value: 'Today', position: 'insideTopRight', fontSize: 11, fill: SERIES.target }} />
            )}
            {events.map((e, i) => {
              const t = parseApi(e.date).getTime()
              return (
                <ReferenceDot
                  key={i} x={t} y={paceAt(t)} ifOverflow="visible"
                  shape={(p: { cx?: number; cy?: number }) => (
                    <g>
                      <rect x={(p.cx ?? 0) - 5} y={(p.cy ?? 0) - 5} width={10} height={10} transform={`rotate(45 ${p.cx ?? 0} ${p.cy ?? 0})`} fill={SERIES.event} stroke="#fff" strokeWidth={1.5} />
                      <title>{`${dayLabel(t)}: ${e.label}`}</title>
                    </g>
                  )}
                />
              )
            })}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {events.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-ink-muted">
          {events.map((e, i) => (
            <li key={i}>
              <span aria-hidden="true">◆</span> {dayLabel(parseApi(e.date).getTime())}: {e.label}
            </li>
          ))}
        </ul>
      )}
    </ChartCard>
  )
}
