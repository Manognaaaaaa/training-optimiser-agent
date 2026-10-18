import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import { Fragment, useMemo, useState, type KeyboardEvent, type ReactNode } from 'react'
import { cn } from '../../lib/cn'

export interface Column<T> {
  key: string
  header: ReactNode
  /** Optional (i) tip shown beside the header. Kept outside the sort button, since a button cannot contain a button. */
  info?: ReactNode
  cell: (row: T) => ReactNode
  /** Give a sort value to make the header clickable. */
  sortValue?: (row: T) => number | string
  className?: string
  align?: 'left' | 'right'
}

interface Props<T> {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string | number
  /** Accessible name for the table. */
  caption: string
  onRowClick?: (row: T) => void
  /** Content shown under the row whose key equals expandedKey. */
  renderExpanded?: (row: T) => ReactNode
  expandedKey?: string | number | null
  /** On narrow screens the table becomes a list of these cards. */
  mobileCard?: (row: T) => ReactNode
  initialSort?: { key: string; dir: 'asc' | 'desc' }
  empty?: ReactNode
  /** Max height with a scrolling body; the header stays visible. */
  maxHeight?: string
}

/** Sortable table with a sticky header, clickable rows (keyboard too) and an optional expanded row. */
export function DataTable<T>({
  columns, rows, rowKey, caption, onRowClick, renderExpanded, expandedKey, mobileCard, initialSort, empty, maxHeight,
}: Props<T>) {
  const [sort, setSort] = useState(initialSort ?? null)

  const sorted = useMemo(() => {
    if (!sort) return rows
    const col = columns.find((c) => c.key === sort.key)
    if (!col?.sortValue) return rows
    const get = col.sortValue
    const factor = sort.dir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      const x = get(a)
      const y = get(b)
      return (x < y ? -1 : x > y ? 1 : 0) * factor
    })
  }, [rows, sort, columns])

  const toggleSort = (key: string) =>
    setSort((cur) => (cur?.key === key ? (cur.dir === 'asc' ? { key, dir: 'desc' } : null) : { key, dir: 'asc' }))

  const onKey = (e: KeyboardEvent, row: T) => {
    if (onRowClick && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault()
      onRowClick(row)
    }
  }

  if (rows.length === 0 && empty) return <>{empty}</>

  return (
    <>
      {mobileCard && (
        <ul className="space-y-2 md:hidden">
          {sorted.map((row) => (
            <li
              key={rowKey(row)}
              className={cn('rounded-panel border border-line bg-surface p-3', onRowClick && 'cursor-pointer')}
              tabIndex={onRowClick ? 0 : undefined}
              onClick={() => onRowClick?.(row)}
              onKeyDown={(e) => onKey(e, row)}
            >
              {mobileCard(row)}
            </li>
          ))}
        </ul>
      )}
      <div className={cn('overflow-auto rounded-panel border border-line bg-surface', mobileCard && 'hidden md:block')} style={{ maxHeight }}>
        <table className="min-w-full border-collapse text-sm">
          <caption className="sr-only">{caption}</caption>
          <thead className="sticky top-0 z-10 bg-canvas">
            <tr>
              {columns.map((c) => {
                const active = sort?.key === c.key
                return (
                  <th
                    key={c.key}
                    scope="col"
                    aria-sort={active ? (sort!.dir === 'asc' ? 'ascending' : 'descending') : undefined}
                    className={cn('border-b border-line px-3 py-2 text-xs font-semibold text-ink-muted', c.align === 'right' ? 'text-right' : 'text-left', c.className)}
                  >
                    <span className="inline-flex items-start gap-1">
                      {c.sortValue ? (
                        <button type="button" className="inline-flex items-center gap-1 text-left hover:text-ink" onClick={() => toggleSort(c.key)}>
                          {c.header}
                          {active ? sort!.dir === 'asc' ? <ArrowUp size={12} aria-hidden="true" /> : <ArrowDown size={12} aria-hidden="true" /> : <ChevronsUpDown size={12} aria-hidden="true" className="opacity-50" />}
                        </button>
                      ) : (
                        c.header
                      )}
                      {c.info}
                    </span>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const key = rowKey(row)
              const open = renderExpanded !== undefined && expandedKey === key
              return (
                <Fragment key={key}>
                  <tr
                    className={cn('border-b border-line align-top last:border-0', onRowClick && 'cursor-pointer hover:bg-canvas', open && 'bg-canvas')}
                    tabIndex={onRowClick ? 0 : undefined}
                    aria-expanded={renderExpanded ? open : undefined}
                    onClick={() => onRowClick?.(row)}
                    onKeyDown={(e) => onKey(e, row)}
                  >
                    {columns.map((c) => (
                      <td key={c.key} className={cn('px-3 py-3', c.align === 'right' && 'text-right', c.className)}>
                        {c.cell(row)}
                      </td>
                    ))}
                  </tr>
                  {open && (
                    <tr className="border-b border-line bg-canvas last:border-0">
                      <td colSpan={columns.length} className="px-4 pb-4 pt-1">
                        {renderExpanded!(row)}
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}
