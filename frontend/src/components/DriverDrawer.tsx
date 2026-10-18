import { useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import {
  addUnavailability, getDriver, listUnavailability, removeUnavailability, type Driver, type DriverBooking,
} from '../api/drivers'
import { useRefresh } from '../hooks/useRefresh'
import { fmtDate, fmtDateTime, toApiDateTime } from '../utils/datetime'
import { ActiveBadge, ShiftBadge, StatusBadge } from './Badge'
import Drawer, { Section } from './Drawer'
import { ErrorBanner, inputClass } from './FormModal'
import { useToast } from './Toast'

function BookingList({ rows, empty }: { rows: DriverBooking[]; empty: string }) {
  if (rows.length === 0) return <p className="text-sm text-slate-500">{empty}</p>
  return (
    <ul className="max-h-56 divide-y divide-slate-100 overflow-y-auto rounded-md border border-slate-200">
      {rows.map((b) => (
        <li key={b.enrollment_id} className="flex items-center gap-3 px-3 py-2 text-sm">
          <span className="w-12 font-medium">{b.course_code}</span>
          <span className="flex-1 text-slate-600">{fmtDateTime(b.session_start)}</span>
          <StatusBadge status={b.status} />
        </li>
      ))}
    </ul>
  )
}

interface Props {
  driverId: number
  onClose: () => void
  onEdit: (driver: Driver) => void
  onDeactivate: (driver: Driver, upcoming: number) => void
}

/** Driver profile, training history, upcoming bookings and unavailability blocks. */
export default function DriverDrawer({ driverId, onClose, onEdit, onDeactivate }: Props) {
  const toast = useToast()
  const refresh = useRefresh()
  const driver = useQuery({ queryKey: ['driver', driverId], queryFn: () => getDriver(driverId) })
  const blocks = useQuery({ queryKey: ['unavailability', driverId], queryFn: () => listUnavailability(driverId) })
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const d = driver.data

  async function add(e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const block = await addUnavailability(driverId, {
        start_time: toApiDateTime(new Date(start)),
        end_time: toApiDateTime(new Date(end)),
        reason,
      })
      await refresh('drivers')
      toast.success(
        block.cancelled_bookings > 0
          ? `Block added. ${block.cancelled_bookings} overlapping booking(s) cancelled.`
          : 'Block added',
      )
      setStart('')
      setEnd('')
      setReason('')
    } catch (err) {
      setError((err as Error).message)
    }
  }

  async function remove(uid: number) {
    try {
      await removeUnavailability(driverId, uid)
      await refresh('drivers')
      toast.success('Block removed')
    } catch (err) {
      toast.error((err as Error).message)
    }
  }

  return (
    <Drawer title={d ? `${d.name} (${d.employee_code})` : 'Driver'} onClose={onClose}>
      {driver.isError && <ErrorBanner message={driver.error.message} />}
      {!d && !driver.isError && <p className="text-sm text-slate-500">Loading…</p>}
      {d && (
        <>
          <Section
            title="Profile"
            action={
              <div className="flex gap-2">
                <button className="rounded-md border border-slate-300 px-3 py-1 text-sm hover:bg-slate-50" onClick={() => onEdit(d)}>Edit</button>
                {d.is_active && (
                  <button className="rounded-md border border-red-300 px-3 py-1 text-sm text-red-700 hover:bg-red-50" onClick={() => onDeactivate(d, d.upcoming.length)}>
                    Deactivate
                  </button>
                )}
              </div>
            }
          >
            <dl className="grid grid-cols-[8rem_1fr] gap-y-1.5 text-sm">
              <dt className="text-slate-500">Nationality</dt><dd>{d.nationality}</dd>
              <dt className="text-slate-500">Shift</dt><dd><ShiftBadge shift={d.shift} /></dd>
              <dt className="text-slate-500">Depot</dt><dd>{d.depot}</dd>
              <dt className="text-slate-500">Hired</dt><dd>{fmtDate(d.hire_date)}</dd>
              <dt className="text-slate-500">Status</dt><dd><ActiveBadge active={d.is_active} /></dd>
            </dl>
          </Section>

          <Section title={`Upcoming bookings (${d.upcoming.length})`}>
            <BookingList rows={d.upcoming} empty="No upcoming bookings." />
          </Section>

          <Section title={`Training history (${d.history.length})`}>
            <BookingList rows={d.history} empty="No training history yet." />
          </Section>

          <Section title="Unavailability">
            {blocks.data?.length === 0 && <p className="mb-2 text-sm text-slate-500">No blocks.</p>}
            <ul className="mb-3 divide-y divide-slate-100 rounded-md border border-slate-200 empty:hidden">
              {blocks.data?.map((b) => (
                <li key={b.id} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <span className="flex-1">{fmtDateTime(b.start_time)} to {fmtDateTime(b.end_time)}</span>
                  <span className="text-slate-500">{b.reason}</span>
                  <button className="text-xs text-red-600 hover:underline" onClick={() => remove(b.id)}>Remove</button>
                </li>
              ))}
            </ul>
            <form onSubmit={add} className="space-y-2 rounded-md bg-slate-50 p-3">
              <ErrorBanner message={error} />
              <div className="grid grid-cols-2 gap-2">
                <label className="text-xs text-slate-600">From<input type="datetime-local" className={inputClass} value={start} onChange={(e) => setStart(e.target.value)} required /></label>
                <label className="text-xs text-slate-600">To<input type="datetime-local" className={inputClass} value={end} onChange={(e) => setEnd(e.target.value)} required /></label>
              </div>
              <div className="flex gap-2">
                <input className={inputClass} placeholder="Reason (e.g. annual leave)" value={reason} onChange={(e) => setReason(e.target.value)} required />
                <button className="rounded-md bg-primary px-3 py-1.5 text-sm text-white hover:bg-primary-hover">Add block</button>
              </div>
              <p className="text-xs text-slate-500">Upcoming bookings that overlap the block are cancelled.</p>
            </form>
          </Section>
        </>
      )}
    </Drawer>
  )
}
