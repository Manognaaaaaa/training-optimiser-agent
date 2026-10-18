import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { isRunActive, listPlannerRuns } from '../../api/planner'
import { advanceSim, getSimState } from '../../api/sim'
import { fmtDate } from '../../lib/format'
import { logAdvance } from '../../lib/simLog'

/** Shared by the top-bar clock and the Simulation page: move the clock, refresh every cache, say what happened. */
export function useAdvance() {
  const queryClient = useQueryClient()
  const state = useQuery({ queryKey: ['sim', 'state'], queryFn: getSimState })
  const runs = useQuery({ queryKey: ['planner', 'runs', 'all'], queryFn: () => listPlannerRuns(), refetchInterval: 3000 })
  const plannerBusy = runs.data?.some((r) => isRunActive(r)) ?? false

  const advance = useMutation({
    mutationFn: (days: number) => advanceSim(days),
    onSuccess: async (summary) => {
      logAdvance(summary)
      await queryClient.invalidateQueries() // every list, chart and drawer is now stale
      const records = summary.attended + summary.no_shows
      toast.success(`Moved to ${fmtDate(summary.to_time)}. ${records} attendance record${records === 1 ? '' : 's'} added.`)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const ended = state.data?.year_ended ?? false
  const disabledReason = plannerBusy ? 'The planner is running, so the clock is paused until it finishes.' : ended ? 'The simulated year has ended.' : ''
  const disabled = advance.isPending || plannerBusy || ended || !state.data
  return { state, advance, plannerBusy, ended, disabledReason, disabled }
}
