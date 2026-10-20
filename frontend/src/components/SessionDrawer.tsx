import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  addEnrollment, cancelSession, getSession, listEligibleDrivers, listEnrollments, removeEnrollment,
} from '../api/sessions'
import { useDebounced } from '../hooks/useDebounced'
import { ProgressBar } from './ui'
import { useRefresh } from '../hooks/useRefresh'
import { fmtDateTime, fmtTime } from '../utils/datetime'
import { FillBadge, ShiftBadge, StatusBadge } from './Badge'
import ConfirmDialog from './ConfirmDialog'
import Drawer, { Section } from './Drawer'
import { inputClass } from './FormModal'
import SessionFormModal from './SessionFormModal'
import { useToast } from './Toast'

/** Details, enrollments and booking for one session. Used by Master Data and the Calendar. */
export default function SessionDrawer({ sessionId, onClose }: { sessionId: number; onClose: () => void }) {
  const toast = useToast()
  const refresh = useRefresh()
  const session = useQuery({ queryKey: ['session', sessionId], queryFn: () => getSession(sessionId) })
  const enrollments = useQuery({ queryKey: ['enrollments', sessionId], queryFn: () => listEnrollments(sessionId) })
  const [editing, setEditing] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [search, setSearch] = useState('')
  const [showPicker, setShowPicker] = useState(false)
  const debounced = useDebounced(search)
  const s = session.data

  const eligible = useQuery({
    queryKey: ['eligible', sessionId, debounced],
    queryFn: () => listEligibleDrivers(sessionId, debounced),
    enabled: showPicker && !!s?.is_editable,
  })

  const book = useMutation({
    mutationFn: (driverId: number) => addEnrollment(sessionId, driverId),
    onSuccess: async (e) => {
      toast.success(`${e.name} booked`)
      await refresh('sessions')
    },
    onError: (e: Error) => toast.error(e.message), // the backend's reason, e.g. "less than 8h rest after the shift"
  })

  const unbook = useMutation({
    mutationFn: (enrollmentId: number) => removeEnrollment(sessionId, enrollmentId),
    onSuccess: async (e) => {
      toast.success(`${e.name} removed`)
      await refresh('sessions')
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const booked = enrollments.data?.filter((e) => e.status === 'booked').length ?? 0

  return (
    <Drawer title={s ? `${s.course_code} · ${fmtDateTime(s.start_time)}` : 'Session'} onClose={onClose}>
      {session.isError && <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{session.error.message}</div>}
      {!s && !session.isError && <p className="text-sm text-slate-500">Loading…</p>}
      {s && (
        <>
          <Section
            title="Details"
            action={
              s.is_editable && (
                <div className="flex gap-2">
                  <button className="rounded-md border border-slate-300 px-3 py-1 text-sm hover:bg-slate-50" onClick={() => setEditing(true)}>Edit</button>
                  <button className="rounded-md border border-red-300 px-3 py-1 text-sm text-red-700 hover:bg-red-50" onClick={() => setCancelling(true)}>Cancel session</button>
                </div>
              )
            }
          >
            <dl className="grid grid-cols-[8rem_1fr] gap-y-1.5 text-sm">
              <dt className="text-slate-500">Course</dt><dd><Link to={`/dashboard/courses/${s.course_id}`} className="text-primary hover:underline">{s.course_name} ({s.course_code})</Link></dd>
              <dt className="text-slate-500">When</dt><dd>{fmtDateTime(s.start_time)} to {fmtTime(s.end_time)}</dd>
              <dt className="text-slate-500">Trainer</dt><dd>{s.trainer_name}</dd>
              <dt className="text-slate-500">Location</dt><dd>{s.location}</dd>
              <dt className="text-slate-500">Status</dt><dd className="flex items-center gap-2"><StatusBadge status={s.status} /> <span className="text-slate-500">source: {s.source}</span></dd>
              <dt className="text-slate-500">Fill</dt>
              <dd className="flex items-center gap-2">
                <div className="w-full space-y-1">
                  <div className="flex items-center gap-2">
                    <FillBadge band={s.fill_band} rate={s.fill_rate} />
                    <span className="text-slate-500">
                      {s.status === 'completed' ? `${s.attended} attended` : `${s.booked} booked`} of {s.capacity} seats
                    </span>
                  </div>
                  <ProgressBar value={s.status === 'completed' ? s.attended : s.booked} max={s.capacity} label="Seats taken" />
                </div>
              </dd>
            </dl>
          </Section>

          <Section title={`Enrollments (${enrollments.data?.length ?? 0})`}>
            {enrollments.data?.length === 0 && <p className="text-sm text-slate-500">Nobody enrolled yet.</p>}
            <ul className="divide-y divide-slate-100 rounded-md border border-slate-200">
              {enrollments.data?.map((e) => (
                <li key={e.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <span className="w-20 text-slate-500">{e.employee_code}</span>
                  <span className="flex-1">{e.name} <span className="text-slate-400">· {e.nationality}</span></span>
                  <ShiftBadge shift={e.shift} />
                  <StatusBadge status={e.status} />
                  {e.status === 'booked' && s.is_editable && (
                    <button className="text-xs text-red-600 hover:underline disabled:opacity-50" disabled={unbook.isPending} onClick={() => unbook.mutate(e.id)}>
                      Remove
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </Section>

          {s.is_editable && (
            <Section
              title="Add driver"
              action={
                <button className="rounded-md bg-primary px-3 py-1 text-sm text-white hover:bg-primary-hover" onClick={() => setShowPicker((v) => !v)}>
                  {showPicker ? 'Hide' : 'Find drivers'}
                </button>
              }
            >
              {booked >= s.capacity && <p className="text-sm text-risk-medium">This session is full ({booked}/{s.capacity}).</p>}
              {showPicker && (
                <div className="space-y-2">
                  <input className={inputClass} placeholder="Search name or code…" value={search} onChange={(e) => setSearch(e.target.value)} autoFocus />
                  <p className="text-xs text-slate-500">Only drivers who pass every check are listed (free, rested, not already trained this year).</p>
                  <ul className="max-h-64 divide-y divide-slate-100 overflow-y-auto rounded-md border border-slate-200">
                    {eligible.isLoading && <li className="px-3 py-2 text-sm text-slate-500">Loading…</li>}
                    {eligible.data?.length === 0 && <li className="px-3 py-2 text-sm text-slate-500">No eligible drivers match.</li>}
                    {eligible.data?.map((d) => (
                      <li key={d.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                        <span className="w-20 text-slate-500">{d.employee_code}</span>
                        <span className="flex-1">{d.name} <span className="text-slate-400">· {d.depot}</span></span>
                        <ShiftBadge shift={d.shift} />
                        <button className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-50 disabled:opacity-50" disabled={book.isPending} onClick={() => book.mutate(d.id)}>
                          Book
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </Section>
          )}

          {editing && <SessionFormModal session={s} onClose={() => setEditing(false)} />}
          {cancelling && (
            <ConfirmDialog
              title="Cancel this session?"
              confirmLabel="Cancel session"
              onClose={() => setCancelling(false)}
              onConfirm={async () => {
                await cancelSession(s.id)
                await refresh('sessions')
                toast.success('Session cancelled')
                setCancelling(false)
              }}
            >
              This will cancel the session and release {booked} booked {booked === 1 ? 'driver' : 'drivers'}. It cannot be undone.
            </ConfirmDialog>
          )}
        </>
      )}
    </Drawer>
  )
}
