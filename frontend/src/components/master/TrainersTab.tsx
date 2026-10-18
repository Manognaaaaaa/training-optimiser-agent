import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { createTrainer, deleteTrainer, listTrainers, updateTrainer, type Trainer } from '../../api/trainers'
import { useDebounced } from '../../hooks/useDebounced'
import { useRefresh } from '../../hooks/useRefresh'
import { useTableState } from '../../hooks/useTableState'
import ConfirmDialog from '../ConfirmDialog'
import DataTable, { type Column } from '../DataTable'
import FormModal, { Field, inputClass } from '../FormModal'
import { useToast } from '../Toast'

function LoadBar({ used, max }: { used: number; max: number }) {
  const pct = Math.min(100, Math.round((used / max) * 100))
  const colour = used > max ? 'bg-red-500' : used === max ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-28 rounded-full bg-slate-200"><div className={`h-2 rounded-full ${colour}`} style={{ width: `${pct}%` }} /></div>
      <span className="text-xs text-slate-600">this week: {used} / {max}</span>
    </div>
  )
}

function TrainerForm({ trainer, onClose }: { trainer?: Trainer; onClose: () => void }) {
  const [name, setName] = useState(trainer?.name ?? '')
  const [max, setMax] = useState(String(trainer?.max_sessions_per_week ?? 5))
  const refresh = useRefresh()
  const toast = useToast()

  async function save() {
    const body = { name, max_sessions_per_week: Number(max) }
    const saved = trainer ? await updateTrainer(trainer.id, body) : await createTrainer(body)
    await refresh('trainers')
    toast.success(trainer ? 'Trainer updated' : 'Trainer added')
    saved.warnings.forEach((w) => toast.warning(w)) // lowering the limit is allowed but we say where it is now exceeded
    onClose()
  }

  return (
    <FormModal title={trainer ? `Edit ${trainer.name}` : 'Add trainer'} onClose={onClose} onSubmit={save}>
      <Field label="Name" name="name"><input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} required /></Field>
      <Field label="Max sessions per week" name="max_sessions_per_week">
        <input type="number" min={1} className={inputClass} value={max} onChange={(e) => setMax(e.target.value)} required />
      </Field>
    </FormModal>
  )
}

export default function TrainersTab() {
  const table = useTableState('name')
  const [search, setSearch] = useState('')
  const q = useDebounced(search)
  const [form, setForm] = useState<{ trainer?: Trainer } | null>(null)
  const [deleting, setDeleting] = useState<Trainer | null>(null)
  const refresh = useRefresh()
  const toast = useToast()
  const params = { q, page: table.page, page_size: table.pageSize, sort: table.sort }
  const list = useQuery({ queryKey: ['trainers', params], queryFn: () => listTrainers(params), placeholderData: keepPreviousData })

  const columns: Column<Trainer>[] = [
    { key: 'name', header: 'Name', sortKey: 'name', render: (t) => <span className="font-medium">{t.name}</span> },
    { key: 'max', header: 'Max / week', sortKey: 'max_sessions_per_week', render: (t) => t.max_sessions_per_week },
    { key: 'load', header: 'Load', render: (t) => <LoadBar used={t.sessions_this_week} max={t.max_sessions_per_week} /> },
    {
      key: 'actions', header: '', render: (t) => (
        <div className="flex gap-3 text-xs">
          <button className="text-slate-700 hover:underline" onClick={() => setForm({ trainer: t })}>Edit</button>
          <button className="text-red-600 hover:underline" onClick={() => setDeleting(t)}>Delete</button>
        </div>
      ),
    },
  ]

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <input className={`${inputClass} !w-64`} placeholder="Search trainer…" value={search} onChange={(e) => { setSearch(e.target.value); table.setPage(1) }} />
        <button className="ml-auto rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover" onClick={() => setForm({})}>Add trainer</button>
      </div>
      {list.isError && <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{list.error.message}</div>}
      <DataTable
        columns={columns} rows={list.data?.items} rowKey={(t) => t.id} total={list.data?.total ?? 0}
        page={table.page} pageSize={table.pageSize} sort={table.sort} onSort={table.setSort} onPage={table.setPage}
        loading={list.isFetching} emptyText="No trainers found."
      />
      {form && <TrainerForm trainer={form.trainer} onClose={() => setForm(null)} />}
      {deleting && (
        <ConfirmDialog
          title={`Delete ${deleting.name}?`} confirmLabel="Delete" onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await deleteTrainer(deleting.id) // a 409 (trainer still has sessions) shows up inside this dialog
            await refresh('trainers')
            toast.success('Trainer deleted')
            setDeleting(null)
          }}
        >
          A trainer can only be deleted if they have no scheduled or completed sessions.
        </ConfirmDialog>
      )}
    </div>
  )
}
