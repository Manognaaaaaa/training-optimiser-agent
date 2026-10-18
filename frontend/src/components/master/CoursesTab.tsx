import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { createCourse, deleteCourse, listCourses, updateCourse, type Course } from '../../api/courses'
import { useRefresh } from '../../hooks/useRefresh'
import { useTableState } from '../../hooks/useTableState'
import { MandatoryBadge } from '../Badge'
import ConfirmDialog from '../ConfirmDialog'
import DataTable, { type Column } from '../DataTable'
import FormModal, { Field, inputClass } from '../FormModal'
import { useToast } from '../Toast'

function CourseForm({ course, onClose }: { course?: Course; onClose: () => void }) {
  const [form, setForm] = useState({
    code: course?.code ?? '',
    name: course?.name ?? '',
    duration_hours: String(course?.duration_hours ?? 4),
    default_capacity: String(course?.default_capacity ?? 12),
    target_completions: course?.target_completions != null ? String(course.target_completions) : '',
  })
  const [mandatory, setMandatory] = useState(course?.is_mandatory ?? false)
  const refresh = useRefresh()
  const toast = useToast()
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value })

  async function save() {
    const body = {
      code: form.code,
      name: form.name,
      duration_hours: Number(form.duration_hours),
      default_capacity: Number(form.default_capacity),
      is_mandatory: mandatory,
      target_completions: form.target_completions === '' ? null : Number(form.target_completions),
    }
    if (course) await updateCourse(course.id, body)
    else await createCourse(body)
    await refresh('courses')
    toast.success(course ? 'Course updated' : 'Course added')
    onClose()
  }

  return (
    <FormModal title={course ? `Edit ${course.code}` : 'Add course'} onClose={onClose} onSubmit={save}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Code" name="code"><input className={inputClass} value={form.code} onChange={set('code')} required /></Field>
        <Field label="Name" name="name"><input className={inputClass} value={form.name} onChange={set('name')} required /></Field>
        <Field label="Duration (hours)" name="duration_hours"><input type="number" className={inputClass} value={form.duration_hours} onChange={set('duration_hours')} required /></Field>
        <Field label="Default capacity" name="default_capacity"><input type="number" className={inputClass} value={form.default_capacity} onChange={set('default_capacity')} required /></Field>
      </div>
      <Field label="Annual target (2026 completions)" name="target_completions">
        <input type="number" min={0} className={inputClass} value={form.target_completions} onChange={set('target_completions')} />
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={mandatory} onChange={(e) => setMandatory(e.target.checked)} /> Mandatory course
      </label>
    </FormModal>
  )
}

export default function CoursesTab() {
  const table = useTableState('code')
  const [form, setForm] = useState<{ course?: Course } | null>(null)
  const [deleting, setDeleting] = useState<Course | null>(null)
  const refresh = useRefresh()
  const toast = useToast()
  const params = { page: table.page, page_size: table.pageSize, sort: table.sort }
  const list = useQuery({ queryKey: ['courses', params], queryFn: () => listCourses(params), placeholderData: keepPreviousData })

  const columns: Column<Course>[] = [
    { key: 'code', header: 'Code', sortKey: 'code', render: (c) => <span className="font-medium">{c.code}</span> },
    { key: 'name', header: 'Name', sortKey: 'name', render: (c) => c.name },
    { key: 'dur', header: 'Duration', sortKey: 'duration_hours', render: (c) => `${c.duration_hours} h` },
    { key: 'cap', header: 'Capacity', sortKey: 'default_capacity', render: (c) => c.default_capacity },
    { key: 'mand', header: 'Type', sortKey: 'is_mandatory', render: (c) => <MandatoryBadge mandatory={c.is_mandatory} /> },
    { key: 'target', header: '2026 target', sortKey: 'target_completions', render: (c) => c.target_completions ?? '—' },
    {
      key: 'actions', header: '', render: (c) => (
        <div className="flex gap-3 text-xs">
          <button className="text-slate-700 hover:underline" onClick={() => setForm({ course: c })}>Edit</button>
          <button className="text-red-600 hover:underline" onClick={() => setDeleting(c)}>Delete</button>
        </div>
      ),
    },
  ]

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-hover" onClick={() => setForm({})}>Add course</button>
      </div>
      {list.isError && <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{list.error.message}</div>}
      <DataTable
        columns={columns} rows={list.data?.items} rowKey={(c) => c.id} total={list.data?.total ?? 0}
        page={table.page} pageSize={table.pageSize} sort={table.sort} onSort={table.setSort} onPage={table.setPage}
        loading={list.isFetching} emptyText="No courses found."
      />
      {form && <CourseForm course={form.course} onClose={() => setForm(null)} />}
      {deleting && (
        <ConfirmDialog
          title={`Delete ${deleting.code}?`} confirmLabel="Delete" onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await deleteCourse(deleting.id) // 409 if the course has any sessions
            await refresh('courses')
            toast.success('Course deleted')
            setDeleting(null)
          }}
        >
          This deletes the course and its 2026 target. Courses that already have sessions cannot be deleted.
        </ConfirmDialog>
      )}
    </div>
  )
}
