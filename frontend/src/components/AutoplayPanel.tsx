import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { getAutoplay, setAutoplay } from '../api/sim'
import { PAUSE_REASONS } from '../lib/liveMap'
import { Button, Panel, SelectField, Tag } from './ui'

/** Auto-play: the simulated clock moves by itself so the app looks alive. Pauses itself when the planner runs. */
export function AutoplayPanel() {
  const queryClient = useQueryClient()
  const state = useQuery({ queryKey: ['sim', 'autoplay'], queryFn: getAutoplay })
  // Local choices; until the user picks one, show what the server has.
  const [stepChoice, setStepChoice] = useState<'day' | 'week' | null>(null)
  const [secondsChoice, setSecondsChoice] = useState<number | null>(null)
  const step = stepChoice ?? state.data?.step ?? 'day'
  const seconds = secondsChoice ?? state.data?.interval_seconds ?? 5

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setAutoplay({ enabled, step, interval_seconds: seconds }),
    onSuccess: (next) => queryClient.setQueryData(['sim', 'autoplay'], next),
    onError: (e: Error) => toast.error(e.message),
  })
  const s = state.data
  const running = s?.enabled ?? false

  return (
    <Panel title="Auto-play">
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-40">
          <SelectField label="Each step" value={step} onChange={(e) => setStepChoice(e.target.value as 'day' | 'week')} disabled={running}>
            <option value="day">One day</option>
            <option value="week">One week</option>
          </SelectField>
        </div>
        <div className="w-44">
          <SelectField label="Every" value={seconds} onChange={(e) => setSecondsChoice(Number(e.target.value))} disabled={running}>
            {[3, 5, 10, 30].map((v) => <option key={v} value={v}>{v} seconds</option>)}
          </SelectField>
        </div>
        <Button variant={running ? 'danger' : 'primary'} onClick={() => toggle.mutate(!running)} disabled={toggle.isPending || state.isPending}>
          {running ? 'Stop auto-play' : 'Start auto-play'}
        </Button>
        {running && <Tag>Running · {s?.steps_done ?? 0} step{s?.steps_done === 1 ? '' : 's'} so far</Tag>}
      </div>
      {!running && s?.paused_reason && (
        <p className="mt-3 text-sm text-risk-medium" role="status">
          Paused: {PAUSE_REASONS[s.paused_reason] ?? s.paused_reason}. Start it again when you are ready.
        </p>
      )}
    </Panel>
  )
}
