import { useState, type ReactNode } from 'react'

export interface ChartTable {
  head: string[]
  rows: (string | number)[][]
}

/** Card around a chart with a title, a note and a "Show as table" toggle (for screen readers and exact numbers). */
export default function ChartCard({
  title,
  note,
  table,
  children,
  className = '',
}: {
  title: string
  note?: string
  table: ChartTable
  children: ReactNode
  className?: string
}) {
  const [asTable, setAsTable] = useState(false)
  return (
    <section className={`rounded-panel border border-line bg-surface p-4 ${className}`}>
      <div className="mb-2 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-heading text-xl font-semibold text-ink">{title}</h3>
          {note && <p className="mt-0.5 text-xs text-ink-muted">{note}</p>}
        </div>
        <button
          type="button"
          aria-pressed={asTable}
          className="shrink-0 rounded-control border border-line px-2 py-1 text-xs text-ink hover:bg-canvas"
          onClick={() => setAsTable(!asTable)}
        >
          {asTable ? 'Show chart' : 'Show as table'}
        </button>
      </div>
      {asTable ? (
        <div className="max-h-80 overflow-auto">
          <table className="min-w-full text-sm">
            <thead className="sticky top-0 bg-slate-50">
              <tr>
                {table.head.map((h) => (
                  <th key={h} className="px-2 py-1 text-left text-xs font-semibold text-slate-500">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {table.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j} className="px-2 py-1 text-slate-700">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        children
      )}
    </section>
  )
}

/** Small white tooltip box shared by all charts. */
export function TooltipBox({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div className="rounded-control border border-line bg-surface px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-semibold text-slate-900">{title}</div>
      {rows.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-4">
          <span className="text-slate-500">{label}</span>
          <span className="font-medium text-slate-900">{value}</span>
        </div>
      ))}
    </div>
  )
}

/** Shape of what Recharts hands to a custom tooltip: ``payload[0].payload`` is the row being hovered. */
export interface TipProps {
  active?: boolean
  payload?: ReadonlyArray<{ payload?: unknown }>
}
