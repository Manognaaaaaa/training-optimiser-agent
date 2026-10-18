import { useState, type ReactNode } from 'react'
import { ConfirmDialog as UiConfirmDialog } from './ui'
import { ErrorState } from './ui'

interface Props {
  title: string
  /** Say what will happen, e.g. "This will cancel 3 upcoming bookings." */
  children: ReactNode
  confirmLabel: string
  onClose: () => void
  /** Throw to keep the dialog open and show the backend message (e.g. a 409). */
  onConfirm: () => Promise<void>
  danger?: boolean
}

export default function ConfirmDialog({ title, children, confirmLabel, onClose, onConfirm, danger = true }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      await onConfirm()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <UiConfirmDialog
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title={title}
      description={children}
      confirmLabel={confirmLabel}
      tone={danger ? 'danger' : 'primary'}
      busy={busy}
      onConfirm={() => void confirm()}
    >
      {error && <ErrorState title="That did not work" message={error} />}
    </UiConfirmDialog>
  )
}
