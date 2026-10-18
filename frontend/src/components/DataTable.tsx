import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import type { KeyboardEvent, ReactNode } from 'react'
import { cn } from '../lib/cn'
import { Button, Skeleton } from './ui'

export interface Column<T> {
  key: string
  header: string
  /** Server-side sort field. Omit to make the column not sortable. */
  sortKey?: string
  render: (row: T) => ReactNode
  className?: string
}

interface Props<T> {
  columns: Column<T>[]
  rows: T[] | undefined
  rowKey: (row: T) => number | string
  total: number
  page: number
  pageSize: number
  /** Current sort, e.g. 'name' or '-name'. */
  sort: string
  onSort: (sort: string) => void
  onPage: (page: number) => void
  loading?: boolean
  emptyText?: string
  /** Name of the table for screen readers, e.g. "Drivers". */
  caption?: string
  onRowClick?: (row: T) => void
}

/** Table with server-side paging and sortable headers (sticky header, keyboard rows, skeleton loading). The parent owns page and sort. */
export default function DataTable<T>({
  columns, rows, rowKey, total, page, pageSize, sort, onSort, onPage, loading, emptyText = 'Nothing to show.', caption = 'Records', onRowClick,
}: Props<T>) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const sortField = sort.replace(/^-/, '')
  const descending = sort.startsWith('-')

  const clickHeader = (sortKey: string) => onSort(sortField === sortKey && !descending ? `-${sortKey}` : sortKey)
  const onKey = (e: KeyboardEvent, row: T) => {
    if (onRowClick && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault()
      onRowClick(row)
    }
  }

  return (
    <div className="rounded-panel border border-line bg-surface">
      <div className="max-h-[70vh] overflow-auto">
        <table className="min-w-full border-collapse text-sm">
          <caption className="sr-only">{caption}</caption>
          <thead className="sticky top-0 z-10 bg-canvas">
            <tr>
              {columns.map((c) => {
                const active = sortField === c.sortKey
                return (
                  <th
                    key={c.key}
                    scope="col"
                    aria-sort={active ? (descending ? 'descending' : 'ascending') : undefined}
                    className={cn('border-b border-line px-3 py-2 text-left text-xs font-semibold text-ink-muted', c.className)}
                  >
                    {c.sortKey ? (
                      <button type="button" className="inline-flex items-center gap-1 hover:text-ink" onClick={() => clickHeader(c.sortKey!)}>
                        {c.header}
                        {active ? descending ? <ArrowDown size={12} aria-hidden="true" /> : <ArrowUp size={12} aria-hidden="true" /> : <ChevronsUpDown size={12} aria-hidden="true" className="opacity-50" />}
                      </button>
                    ) : (
                      c.header
                    )}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody className={cn(loading && rows && 'opacity-60')}>
            {rows === undefined &&
              [0, 1, 2, 3, 4].map((i) => (
                <tr key={i} className="border-b border-line">
                  <td colSpan={columns.length} className="px-3 py-3"><Skeleton className="h-5 w-full" /></td>
                </tr>
              ))}
            {rows?.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="px-3 py-10 text-center text-sm text-ink-muted">{emptyText}</td>
              </tr>
            )}
            {rows?.map((row) => (
              <tr
                key={rowKey(row)}
                className={cn('border-b border-line last:border-0', onRowClick && 'cursor-pointer hover:bg-canvas')}
                tabIndex={onRowClick ? 0 : undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                onKeyDown={(e) => onKey(e, row)}
              >
                {columns.map((c) => (
                  <td key={c.key} className={cn('px-3 py-2.5', c.className)}>{c.render(row)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-3 py-2 text-sm text-ink-muted">
        <span>{total === 0 ? 'No results' : `Showing ${(page - 1) * pageSize + 1} to ${Math.min(page * pageSize, total)} of ${total}`}</span>
        <div className="flex items-center gap-2">
          <Button size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</Button>
          <span>Page {page} of {pages}</span>
          <Button size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</Button>
        </div>
      </div>
    </div>
  )
}
