import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { deactivateDriver, getDriverOptions, listDrivers, type Driver } from '../../api/drivers'
import { useDebounced } from '../../hooks/useDebounced'
import { useRefresh } from '../../hooks/useRefresh'
import { useTableState } from '../../hooks/useTableState'
import { fmtDate } from '../../utils/datetime'
import { ActiveBadge, ShiftBadge } from '../Badge'
import ConfirmDialog from '../ConfirmDialog'
import DataTable, { type Column } from '../DataTable'
import DriverDrawer from '../DriverDrawer'
import DriverFormModal from '../DriverFormModal'
import { inputClass } from '../FormModal'
import { useToast } from '../Toast'

export default function DriversTab() {
  const table = useTableState('employee_code')
  const [search, setSearch] = useState('')
  const [nationality, setNationality] = useState('')
  const [shift, setShift] = useState('')
  const [depot, setDepot] = useState('')
  const [active, setActive] = useState('')
  const q = useDebounced(search)
  const [openId, setOpenId] = useState<number | null>(null)
  const [form, setForm] = useState<{ driver?: Driver } | null>(null)
  const [deactivating, setDeactivating] = useState<{ driver: Driver; upcoming: number | null } | null>(null)
  const toast = useToast()
  const refresh = useRefresh()

  const options = useQuery({ queryKey: ['driver-options'], queryFn: getDriverOptions }).data
  const params = { q, nationality, shift, depot, is_active: active, page: table.page, page_size: table.pageSize, sort: table.sort }
  const list = useQuery({ queryKey: ['drivers', params], queryFn: () => listDrivers(params), placeholderData: keepPreviousData })

  // Filters go back to page 1 when they change.
  const filter = (set: (v: string) => void) => (e: { target: { value: string } }) => {
    set(e.target.value)
    table.setPage(1)
  }

  const columns: Column<Driver>[] = [
    { key: 'code', header: 'Code', sortKey: 'employee_code', render: (d) => d.employee_code },
    { key: 'name', header: 'Name', sortKey: 'name', render: (d) => <span className="font-medium">{d.name}</span> },
    { key: 'nat', header: 'Nationality', sortKey: 'nationality', render: (d) => d.nationality },
    { key: 'shift', header: 'Shift', sortKey: 'shift', render: (d) => <ShiftBadge shift={d.shift} /> },
    { key: 'depot', header: 'Depot', sortKey: 'depot', render: (d) => d.depot },
    { key: 'hire', header: 'Hired', sortKey: 'hire_date', render: (d) => fmtDate(d.hire_date) },
    { key: 'active', header: 'Status', sortKey: 'is_active', render: (d) => <ActiveBadge active={d.is_active} /> },
    {
      key: 'actions', header: '', render: (d) => (
        <div className="flex gap-3 text-xs" onClick={(e) => e.stopPropagation()}>
          <button className="text-slate-700 hover:underline" onClick={() => setForm({ driver: d })}>Edit</button>
          {d.is_active && <button className="text-red-600 hover:underline" onClick={() => setDeactivating({ driver: d, upcoming: null })}>Deactivate</button>}
        </div>
      ),
    },
  ]

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <input className={`${inputClass} !w-64`} placeholder="Search name or code…" value={search} onChange={filter(setSearch)} />
        <select className={`${inputClass} !w-44`} value={nationality} onChange={filter(setNationality)}>
          <option value="">All nationalities</option>
          {options?.nationalities.map((n) => <option key={n}>{n}</option>)}
        </select>
        <select className={`${inputClass} !w-36`} value={shift} onChange={filter(setShift)}>
          <option value="">All shifts</option>
          {options?.shifts.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select className={`${inputClass} !w-44`} value={depot} onChange={filter(setDepot)}>
          <option value="">All depots</option>
          {options?.depots.map((d) => <option key={d}>{d}</option>)}
        </select>
        <select className={`${inputClass} !w-32`} value={active} onChange={filter(setActive)}>
          <option value="">Any status</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
        <button className="ml-auto rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover" onClick={() => setForm({})}>
          Add driver
        </button>
      </div>
      {list.isError && <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{list.error.message}</div>}
      <DataTable
        columns={columns} rows={list.data?.items} rowKey={(d) => d.id} total={list.data?.total ?? 0}
        page={table.page} pageSize={table.pageSize} sort={table.sort} onSort={table.setSort} onPage={table.setPage}
        loading={list.isFetching} emptyText="No drivers match these filters." onRowClick={(d) => setOpenId(d.id)}
      />

      {openId !== null && (
        <DriverDrawer
          driverId={openId} onClose={() => setOpenId(null)}
          onEdit={(driver) => setForm({ driver })}
          onDeactivate={(driver, upcoming) => setDeactivating({ driver, upcoming })}
        />
      )}
      {form && <DriverFormModal driver={form.driver} onClose={() => setForm(null)} />}
      {deactivating && (
        <ConfirmDialog
          title={`Deactivate ${deactivating.driver.name}?`} confirmLabel="Deactivate" onClose={() => setDeactivating(null)}
          onConfirm={async () => {
            const result = await deactivateDriver(deactivating.driver.id)
            await refresh('drivers')
            toast.success(`${result.name} deactivated. ${result.cancelled_bookings} upcoming booking(s) cancelled.`)
            setDeactivating(null)
          }}
        >
          {deactivating.upcoming === null
            ? 'This marks the driver inactive and cancels all their upcoming bookings. Their training history is kept.'
            : `This marks the driver inactive and cancels ${deactivating.upcoming} upcoming booking(s). Their training history is kept.`}
        </ConfirmDialog>
      )}
    </div>
  )
}
