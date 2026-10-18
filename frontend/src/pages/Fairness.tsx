import { EmptyState, PageHeader } from '../components/ui'

/** Waiting for a fairness endpoint (Phases 6 and 8): there are no approved plans to measure yet. */
export default function Fairness() {
  return (
    <div className="space-y-6">
      <PageHeader title="Fairness" lead="Checks that the work of rescheduling is not piled onto one group of drivers." />
      <EmptyState title="Fairness results will appear once plans are approved">
        When trainers approve plans, this page will show how many drivers from each shift and nationality were moved or newly booked, next to their share of all drivers, and flag any group carrying more than its fair share.
      </EmptyState>
      <section className="rounded-panel border border-line bg-surface p-4 text-sm text-ink">
        <h2 className="font-heading text-xl font-semibold">What you can see today</h2>
        <p className="mt-2">
          Each course page already shows who is behind on that course, by shift and by nationality, in the section "Who is behind". It describes who has completed the course so far, not who has been moved.
        </p>
      </section>
    </div>
  )
}
