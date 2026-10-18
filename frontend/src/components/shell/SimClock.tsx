import { CalendarClock, ChevronsRight, StepForward } from 'lucide-react'
import { fmtDate } from '../../lib/format'
import { Hint, InfoTip } from '../ui'
import { useAdvance } from './useAdvance'

const STEPS = [
  { label: 'Next day', days: 1, Icon: StepForward },
  { label: 'Next week', days: 7, Icon: ChevronsRight },
]

/**
 * The simulated clock: the one bold, yellow element in the shell. The whole app runs on this date, not the real one.
 * Buttons move it forward; they are disabled while the planner works (the backend would answer 409).
 */
export function SimClock() {
  const { state, advance, plannerBusy, disabledReason, disabled } = useAdvance()

  return (
    <div className="flex w-full items-center justify-between gap-2 rounded-panel bg-signal px-3 py-1.5 text-ink md:w-auto md:justify-start md:gap-3">
      <CalendarClock size={18} aria-hidden="true" />
      <div className="min-w-0 leading-tight">
        <div className="flex items-center gap-1 text-xs font-medium">
          Simulated date <InfoTip term="sim_clock" />
        </div>
        <div className="whitespace-nowrap font-heading text-xl font-bold" aria-live="polite">
          {state.data ? fmtDate(state.data.current_time) : '…'}
        </div>
      </div>
      <div className="flex gap-1.5">
        {STEPS.map(({ label, days, Icon }) => {
          const button = (
            <button
              type="button"
              disabled={disabled}
              onClick={() => advance.mutate(days)}
              className="inline-flex items-center gap-1 whitespace-nowrap rounded-control bg-ink px-2.5 py-1.5 text-xs font-medium text-white hover:bg-ink/85 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Icon size={14} aria-hidden="true" />
              {advance.isPending && advance.variables === days ? 'Moving…' : label}
            </button>
          )
          return disabledReason ? <Hint key={label} text={disabledReason}>{button}</Hint> : <span key={label}>{button}</span>
        })}
      </div>
      <span
        className={`h-2.5 w-2.5 rounded-full ${plannerBusy ? 'bg-risk-high' : 'bg-risk-ok'}`}
        title={plannerBusy ? 'Planner running' : 'Clock ready'}
        role="img"
        aria-label={plannerBusy ? 'Planner running' : 'Clock ready'}
      />
    </div>
  )
}
