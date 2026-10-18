import { Link } from 'react-router-dom'
import { EmptyState, PageHeader } from '../components/ui'

/**
 * Waiting for an audit endpoint (Phase 8). The backend already writes an audit row for every decision
 * (alerts, planner runs, plans), but nothing serves them to the UI yet, so there is nothing real to list.
 */
export default function AuditLog() {
  return (
    <div className="space-y-6">
      <PageHeader title="Audit log" lead="A record of every decision the system, the AI planner and trainers make, so any change can be explained later." />
      <EmptyState title="The audit log page is coming in Phase 8" action={<Link to="/alerts" className="text-sm font-medium text-primary hover:underline">Go to alerts</Link>}>
        The system already records these events in the background. Here you will be able to read them as a timeline, grouped by day, filter by type, who did it (the AI planner, a trainer or the system), course and date, and open any entry to see exactly why it happened.
      </EmptyState>
      <section className="rounded-panel border border-line bg-surface p-4 text-sm text-ink">
        <h2 className="font-heading text-xl font-semibold">What will be recorded here</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          <li>Alerts opening, getting worse, getting better or resolving.</li>
          <li>Each time the AI planner ran, and every plan it proposed, with the rules each plan broke.</li>
          <li>Approvals and rejections by trainers, with their reason.</li>
          <li>Changes pushed to the calendar.</li>
        </ul>
      </section>
    </div>
  )
}
