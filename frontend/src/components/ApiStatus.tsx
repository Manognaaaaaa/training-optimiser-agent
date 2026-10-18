import { useEffect, useState } from 'react'
import { getHealth } from '../api/client'
import { cn } from '../lib/cn'
import { Hint } from './ui'

const POLL_MS = 10_000

/** A small dot in the top bar: green when /health says ok, red when the backend is unreachable. Rechecks every 10 s. */
export default function ApiStatus() {
  const [online, setOnline] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    async function check() {
      try {
        const health = await getHealth()
        if (!cancelled) setOnline(health.status === 'ok' && health.database === 'ok')
      } catch {
        if (!cancelled) setOnline(false)
      }
    }
    check()
    const timer = setInterval(check, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const label = online === null ? 'Checking the API…' : online ? 'API connected' : 'API offline: is the backend running on port 8000?'
  return (
    <Hint text={label}>
      <span
        role="img"
        aria-label={label}
        className={cn('block h-3 w-3 rounded-full', online === null ? 'bg-risk-unknown' : online ? 'bg-risk-ok' : 'bg-risk-high')}
      />
    </Hint>
  )
}
