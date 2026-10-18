import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { listSessions, type Session } from '../../api/sessions'
import { useAllCourses, useAllTrainers } from '../../hooks/useLookups'
import { useTableState } from '../../hooks/useTableState'
import { fmtDateTime } from '../../utils/datetime'
import { FillBadge, StatusBadge } from '../Badge'
import DataTable, { type Column } from '../DataTable'
import { inputClass } from '../FormModal'
import SessionDrawer from '../SessionDrawer'
import SessionFormModal from '../SessionFormModal'

export default function SessionsTab() {
  const table = useTableState('start_time')
  const [courseId, setCourseId] = useState('')
  const [trainerId, setTrainerId] = useState('')
  const [status, setStatus] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const courses = useAllCourses().data?.items ?? []
  const trainers = useAllTrainers().data?.items ?? []

  const params = {
    course_id: courseId, trainer_id: trainerId, status,
    start_from: from ? `${from}T00:00:00` : '', start_to: to ? `${to}T23:59:59` : '',
    page: table.page, page_size: table.pageSize, sort: table.sort,
  }
  const list = useQuery({ queryKey: ['sessions', params], queryFn: () => listSessions(params), placeholderData: keepPreviousData })
  const filter = (set: (v: string) => void) => (e: { target: { value: string } }) => {
    set(e.target.value)
    table.setPage(1)
  }

  const columns: Column<Session>[] = [
    { key: 'when', header: 'Date / time', sortKey: 'start_time', render: (s) => fmtDateTime(s.start_time) },
    { key: 'course', header: 'Course', sortKey: 'course', render: (s) => <span className="font-medium">{s.course_code}</span> },
    { key: 'trainer', header: 'Trainer', sortKey: 'trainer', render: (s) => s.trainer_name },
    { key: 'loc', header: 'Location', sortKey: 'location', render: (s) => s.location },
    { key: 'booked', header: 'Booked', sortKey: 'booked', render: (s) => `${s.booked} / ${s.capacity}` },
    { key: 'fill', header: 'Fill', render: (s) => <FillBadge band={s.fill_band} rate={s.fill_rate} /> },
    { key: 'status', header: 'Status', sortKey: 'status', render: (s) => <StatusBadge status={s.status} /> },
    { key: 'source', header: 'Source', sortKey: 'source', render: (s) => s.source },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <select className={`${inputClass} !w-52`} value={courseId} onChange={filter(setCourseId)}>
          <option value="">All courses</option>
          {courses.map((c) => <option key={c.id} value={c.id}>{c.code} · {c.name}</option>)}
        </select>
        <select className={`${inputClass} !w-48`} value={trainerId} onChange={filter(setTrainerId)}>
          <option value="">All trainers</option>
          {trainers.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <select className={`${inputClass} !w-36`} value={status} onChange={filter(setStatus)}>
          <option value="">Any status</option>
          <option>scheduled</option>
          <option>completed</option>
          <option>cancelled</option>
        </select>
        <label className="text-xs text-slate-600">From<input type="date" className={`${inputClass} !w-40`} value={from} onChange={filter(setFrom)} /></label>
        <label className="text-xs text-slate-600">To<input type="date" className={`${inputClass} !w-40`} value={to} onChange={filter(setTo)} /></label>
        <button className="ml-auto rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover" onClick={() => setCreating(true)}>
          Add session
        </button>
      </div>
      {list.isError && <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{list.error.message}</div>}
      <DataTable
        columns={columns} rows={list.data?.items} rowKey={(s) => s.id} total={list.data?.total ?? 0}
        page={table.page} pageSize={table.pageSize} sort={table.sort} onSort={table.setSort} onPage={table.setPage}
        loading={list.isFetching} emptyText="No sessions match these filters." onRowClick={(s) => setOpenId(s.id)}
      />
      {openId !== null && <SessionDrawer sessionId={openId} onClose={() => setOpenId(null)} />}
      {creating && <SessionFormModal onClose={() => setCreating(false)} onSaved={(s) => setOpenId(s.id)} />}
    </div>
  )
}
