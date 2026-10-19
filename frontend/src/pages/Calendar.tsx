import dayGridPlugin from '@fullcalendar/daygrid'
import type { DatesSetArg, EventContentArg, EventInput } from '@fullcalendar/core'
import interactionPlugin, { type DateClickArg } from '@fullcalendar/interaction'
import listPlugin from '@fullcalendar/list'
import FullCalendar from '@fullcalendar/react'
import timeGridPlugin from '@fullcalendar/timegrid'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useMemo, useRef, useState } from 'react'
import { getCalendar, type CalendarEvent } from '../api/sessions'
import { getSimState } from '../api/sim'
import SessionDrawer from '../components/SessionDrawer'
import { SyncIcon } from '../components/SyncBadge'
import SessionFormModal from '../components/SessionFormModal'
import { Button, ErrorState, PageHeader, SelectField, Skeleton } from '../components/ui'
import { useAllCourses, useAllTrainers } from '../hooks/useLookups'
import { cn } from '../lib/cn'
import { fmtDateTime } from '../lib/format'
import { toApiDateTime } from '../utils/datetime'

/** Four fill levels plus cancelled. "Full" is worked out here: the backend only has low / medium / high. */
type Band = 'low' | 'medium' | 'high' | 'full' | 'cancelled'
const bandOf = (e: CalendarEvent): Band => (e.fill_band === 'cancelled' ? 'cancelled' : (e.fill_rate ?? 0) >= 1 ? 'full' : e.fill_band)

const LEGEND: { band: Band; label: string; swatch: string }[] = [
  { band: 'low', label: 'Under 50% full', swatch: 'bg-risk-medium-bg border-risk-medium' },
  { band: 'medium', label: '50 to 79% full', swatch: 'bg-primary-soft border-primary' },
  { band: 'high', label: '80% or more', swatch: 'bg-risk-ok-bg border-risk-ok' },
  { band: 'full', label: 'Full', swatch: 'bg-primary border-primary' },
  { band: 'cancelled', label: 'Cancelled', swatch: 'bg-risk-unknown-bg border-ink-muted' },
]

function eventContent(arg: EventContentArg) {
  const e = arg.event.extendedProps as CalendarEvent
  const count = e.status === 'completed' ? e.attended : e.booked
  return (
    <div className="overflow-hidden px-1 text-xs leading-tight">
      <div className="flex items-center justify-between gap-1 font-semibold">
        <span>{e.course_code} {count}/{e.capacity}</span>
        <SyncIcon info={e.calendar} />
      </div>
      <div className="truncate opacity-80">{e.trainer_name}</div>
    </div>
  )
}

function CalendarView({ simNow }: { simNow: string }) {
  const calendarRef = useRef<FullCalendar>(null)
  const [range, setRange] = useState<{ start: string; end: string } | null>(null)
  const [courseId, setCourseId] = useState('')
  const [trainerId, setTrainerId] = useState('')
  const [location, setLocation] = useState('')
  const [lowOnly, setLowOnly] = useState(false)
  const [showCancelled, setShowCancelled] = useState(false)
  const [openId, setOpenId] = useState<number | null>(null)
  const [newStart, setNewStart] = useState<Date | null>(null)
  const courses = useAllCourses().data?.items ?? []
  const trainers = useAllTrainers().data?.items ?? []

  const params = { start: range?.start ?? '', end: range?.end ?? '', course_id: courseId, trainer_id: trainerId, include_cancelled: showCancelled }
  const calendar = useQuery({ queryKey: ['calendar', params], queryFn: () => getCalendar(params), enabled: range !== null, placeholderData: keepPreviousData })

  const locations = useMemo(() => [...new Set((calendar.data?.events ?? []).map((e) => e.location))].sort(), [calendar.data])
  const events = useMemo<EventInput[]>(
    () =>
      (calendar.data?.events ?? [])
        .filter((e) => (!location || e.location === location) && (!lowOnly || (e.fill_band === 'low' && e.status === 'scheduled')))
        .map((e) => ({
          id: String(e.id), start: e.start, end: e.end,
          classNames: [`fc-band-${bandOf(e)}`, e.status === 'completed' ? 'fc-completed' : ''],
          extendedProps: e,
        })),
    [calendar.data, location, lowOnly],
  )

  function onDatesSet(arg: DatesSetArg) {
    const next = { start: toApiDateTime(arg.start), end: toApiDateTime(arg.end) }
    setRange((prev) => (prev && prev.start === next.start && prev.end === next.end ? prev : next))
  }

  function onDateClick(arg: DateClickArg) {
    if (arg.date <= new Date(simNow)) return // only future slots; the backend still has the final say
    const when = new Date(arg.date)
    if (arg.allDay) when.setHours(9, 0, 0, 0) // month view gives midnight: default to the first slot
    setNewStart(when)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-56">
          <SelectField label="Course" value={courseId} onChange={(e) => setCourseId(e.target.value)}>
            <option value="">All courses</option>
            {courses.map((c) => <option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}
          </SelectField>
        </div>
        <div className="w-48">
          <SelectField label="Trainer" value={trainerId} onChange={(e) => setTrainerId(e.target.value)}>
            <option value="">All trainers</option>
            {trainers.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </SelectField>
        </div>
        {locations.length > 1 && (
          <div className="w-48">
            <SelectField label="Location" value={location} onChange={(e) => setLocation(e.target.value)}>
              <option value="">All locations</option>
              {locations.map((l) => <option key={l} value={l}>{l}</option>)}
            </SelectField>
          </div>
        )}
        <label className="flex items-center gap-2 pb-2 text-sm"><input type="checkbox" checked={lowOnly} onChange={(e) => setLowOnly(e.target.checked)} /> Low fill only</label>
        <label className="flex items-center gap-2 pb-2 text-sm"><input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} /> Show cancelled</label>
        <Button className="ml-auto" onClick={() => calendarRef.current?.getApi().gotoDate(new Date(simNow))}>Go to today</Button>
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-panel border border-line bg-surface px-4 py-2 text-xs text-ink-muted">
        {LEGEND.map((l) => (
          <span key={l.band} className="flex items-center gap-1.5"><span className={cn('inline-block h-3 w-3 rounded-sm border', l.swatch)} />{l.label}</span>
        ))}
        <span className="flex items-center gap-1.5"><span className="inline-block h-0.5 w-4 bg-signal" /> Today (simulated)</span>
        <span className="flex items-center gap-1.5"><span className="fc-completed inline-block h-3 w-3 rounded-sm border border-ink-muted bg-line" /> Striped: already held</span>
      </div>
      {calendar.isError && <ErrorState title="The calendar could not load" message={calendar.error.message} onRetry={() => void calendar.refetch()} />}

      <div className="rounded-panel border border-line bg-surface p-3">
        <FullCalendar
          ref={calendarRef}
          plugins={[dayGridPlugin, timeGridPlugin, listPlugin, interactionPlugin]}
          initialView="timeGridWeek"
          initialDate={simNow}
          now={simNow}
          nowIndicator
          timeZone="local"
          firstDay={0}
          slotMinTime="07:00:00"
          slotMaxTime="22:00:00"
          allDaySlot={false}
          height="auto"
          validRange={{ start: '2026-01-01', end: '2027-01-01' }}
          headerToolbar={{ left: 'prev,next', center: 'title', right: 'timeGridWeek,dayGridMonth,listWeek' }}
          buttonText={{ timeGridWeek: 'Week', dayGridMonth: 'Month', listWeek: 'List' }}
          slotLabelFormat={{ hour: '2-digit', minute: '2-digit', hour12: false }}
          eventTimeFormat={{ hour: '2-digit', minute: '2-digit', hour12: false }}
          events={events}
          eventDisplay="block"
          eventContent={eventContent}
          eventClick={(arg) => setOpenId(Number(arg.event.id))}
          dateClick={onDateClick}
          datesSet={onDatesSet}
        />
      </div>

      {openId !== null && <SessionDrawer sessionId={openId} onClose={() => setOpenId(null)} />}
      {newStart && <SessionFormModal initialStart={newStart} onClose={() => setNewStart(null)} onSaved={(s) => setOpenId(s.id)} />}
    </div>
  )
}

export default function Calendar() {
  const sim = useQuery({ queryKey: ['sim', 'state'], queryFn: getSimState })
  return (
    <div className="space-y-6">
      <PageHeader
        title="Calendar"
        lead={
          <>
            Training sessions, coloured by how full they are. The yellow line is today in the simulation
            {sim.data && ` (${fmtDateTime(sim.data.current_time)})`}. Click a session to manage it, or an empty future slot to add one.
          </>
        }
      />
      {sim.isError && <ErrorState title="The simulated date could not load" message={sim.error.message} onRetry={() => void sim.refetch()} />}
      {sim.data ? <CalendarView simNow={sim.data.current_time} /> : !sim.isError && <Skeleton className="h-96 w-full" />}
    </div>
  )
}
