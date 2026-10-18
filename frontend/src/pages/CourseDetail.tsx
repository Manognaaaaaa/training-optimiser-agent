import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getCourseDetail, type Reason } from '../api/tracking'
import { Badge, FillBadge, MandatoryBadge, RiskBadge } from '../components/Badge'
import CourseChart from '../components/charts/CourseChart'
import ForecastHistory from '../components/charts/ForecastHistory'
import GroupBars from '../components/charts/GroupBars'
import WeeklyBars from '../components/charts/WeeklyBars'
import PlannerPanel from '../components/PlannerPanel'
import SessionDrawer from '../components/SessionDrawer'
import { fmtDateTime } from '../utils/datetime'
import { SHORTFALL_LABEL, fmtProb, fmtRange, shortfallSentence } from '../utils/tracking'

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-lg font-semibold text-slate-900">{value}</div>
    </div>
  )
}

function ReasonsPanel({ reasons }: { reasons: Reason[] }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4">
      <h3 className="text-sm font-semibold text-slate-900">Why this course is at risk</h3>
      {reasons.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">No risk reasons: the course is on track, achieved, or does not have enough data yet.</p>
      ) : (
        <ol className="mt-2 divide-y divide-slate-100">
          {reasons.map((r, i) => (
            <li key={r.code} className="flex items-start justify-between gap-4 py-2">
              <div>
                <div className="text-sm text-slate-900">
                  {i + 1}. {r.message}
                </div>
                <div className="text-xs text-slate-500">{r.code.replace(/_/g, ' ')}</div>
              </div>
              <div className="shrink-0 text-right text-xs text-slate-500">
                <div>
                  value <span className="font-medium text-slate-900">{Number(r.value.toFixed(1))}</span>
                </div>
                <div>
                  benchmark <span className="font-medium text-slate-900">{Number(r.benchmark.toFixed(1))}</span>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

export default function CourseDetail() {
  const { id } = useParams()
  const courseId = Number(id)
  const [sessionId, setSessionId] = useState<number | null>(null)
  const detail = useQuery({ queryKey: ['tracking', 'course', courseId], queryFn: () => getCourseDetail(courseId), enabled: Number.isFinite(courseId) })

  if (detail.isPending) return <div className="text-sm text-slate-500">Loading…</div>
  if (detail.isError) {
    return (
      <div className="space-y-3">
        <Link to="/" className="text-sm text-slate-600 underline">← Dashboard</Link>
        <div className="rounded-md bg-red-50 px-4 py-3 text-sm text-red-700">{detail.error.message}</div>
      </div>
    )
  }
  const d = detail.data
  const c = d.course

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="text-sm text-slate-600 underline">← Dashboard</Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-slate-900">{c.name}</h1>
          <span className="text-sm text-slate-400">{c.code}</span>
          <MandatoryBadge mandatory={c.is_mandatory} />
          <RiskBadge level={c.risk_level} />
          {c.shortfall_type !== 'none' && <Badge>{SHORTFALL_LABEL[c.shortfall_type]} gap</Badge>}
        </div>
        <p className="mt-2 max-w-3xl text-sm text-slate-700">{shortfallSentence(c)}</p>
      </div>

      <div className="grid grid-cols-2 gap-4 rounded-lg border border-slate-200 bg-white p-4 sm:grid-cols-3 xl:grid-cols-6">
        <Stat label="Attended / target" value={`${c.attended} / ${c.target}`} />
        <Stat label="Projected (90%)" value={fmtRange(c)} />
        <Stat label="P(hit target)" value={fmtProb(c.p_hit)} />
        <Stat label="Show-up rate" value={`${Math.round(c.p_show * 100)}%`} />
        <Stat label="Seats left" value={`${c.remaining_seats} in ${c.remaining_sessions} sessions`} />
        <Stat label="Eligible drivers" value={String(c.eligible_pool)} />
      </div>

      <CourseChart series={d.series} target={c.target} events={d.events} />
      <ReasonsPanel reasons={c.reasons} />
      <PlannerPanel courseId={courseId} />

      <div className="grid gap-4 xl:grid-cols-2">
        <GroupBars title="Who is behind: by shift" rows={d.breakdown.shift} />
        <GroupBars title="Who is behind: by nationality" rows={d.breakdown.nationality} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <ForecastHistory snapshots={d.snapshots} target={c.target} events={d.events} />
        <WeeklyBars weeks={d.weekly} />
      </div>

      <section>
        <h2 className="mb-2 text-lg font-semibold">Upcoming sessions</h2>
        {d.upcoming_sessions.length === 0 ? (
          <p className="text-sm text-slate-500">No sessions left on the calendar for this course.</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full divide-y divide-slate-200 text-sm">
              <thead className="bg-slate-50">
                <tr>
                  {['Start', 'Booked / capacity', 'Fill'].map((h) => (
                    <th key={h} className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {d.upcoming_sessions.map((s) => (
                  <tr key={s.id} className="cursor-pointer hover:bg-slate-50" onClick={() => setSessionId(s.id)}>
                    <td className="px-3 py-2">{fmtDateTime(s.start_time)}</td>
                    <td className="px-3 py-2">
                      {s.booked} / {s.capacity}
                    </td>
                    <td className="px-3 py-2">
                      <FillBadge band={s.fill_band} rate={s.fill_rate} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {sessionId !== null && <SessionDrawer sessionId={sessionId} onClose={() => setSessionId(null)} />}
    </div>
  )
}
