import * as Dialog from '@radix-ui/react-dialog'
import { Children, cloneElement, createContext, isValidElement, useContext, useId, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError } from '../api/client'
import { cn } from '../lib/cn'
import { Button, ErrorState } from './ui'

const FieldErrors = createContext<Record<string, string>>({})

/** Shared classes for inputs inside forms. */
export const inputClass =
  'w-full rounded-control border border-line bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted disabled:bg-canvas disabled:text-ink-muted'

/** True when a child input is marked `required`, so the label can show the required marker. */
const hasRequiredChild = (children: ReactNode) =>
  Children.toArray(children).some((c) => isValidElement(c) && Boolean((c.props as { required?: boolean }).required))

/** A labelled form field. `name` is the backend field name, used to show a 422 message under it. */
export function Field({ label, name, children }: { label: string; name: string; children: ReactNode }) {
  const errors = useContext(FieldErrors)
  const id = useId()
  const required = hasRequiredChild(children)
  return (
    <div className="block text-sm">
      <label htmlFor={id} className="mb-1 block font-medium text-ink">
        {label}
        {required && <span className="ml-0.5 text-risk-high" aria-hidden="true">*</span>}
        {required && <span className="sr-only"> (required)</span>}
      </label>
      <FieldInput id={id} invalid={Boolean(errors[name])}>{children}</FieldInput>
      {errors[name] && <span role="alert" className="mt-1 block text-xs text-risk-high">{errors[name]}</span>}
    </div>
  )
}

/** Gives the first input child the label's id (and marks it invalid) without changing how forms are written. */
function FieldInput({ id, invalid, children }: { id: string; invalid: boolean; children: ReactNode }) {
  const items = Children.toArray(children)
  const isControl = (c: ReactNode) => isValidElement(c) && typeof c.type === 'string' && ['input', 'select', 'textarea'].includes(c.type)
  const firstControl = items.findIndex(isControl)
  return (
    <>
      {items.map((child, i) => {
        if (i !== firstControl || !isValidElement(child)) return child
        const props = child.props as { className?: string }
        return cloneElement(child as React.ReactElement<Record<string, unknown>>, {
          id, 'aria-invalid': invalid, className: cn(props.className, invalid && 'border-risk-high'),
        })
      })}
    </>
  )
}

/** Red banner used for 409 / 422 messages from the backend. */
export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null
  return <ErrorState title="That did not work" message={message} />
}

interface Props {
  title: string
  submitLabel?: string
  onClose: () => void
  /** Do the save. Throw (the api client does) to keep the modal open and show the message. */
  onSubmit: () => Promise<void>
  children: ReactNode
}

/** Modal form. A failed submit shows the backend `detail` in a banner and marks fields from 422 errors. */
export default function FormModal({ title, submitLabel = 'Save', onClose, onSubmit, children }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setFieldErrors({})
    try {
      await onSubmit()
    } catch (err) {
      setError((err as Error).message)
      if (err instanceof ApiError) setFieldErrors(err.fieldErrors)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog.Root open onOpenChange={(open) => !open && !busy && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-ink/40 data-[state=open]:animate-[fade-in_0.15s_ease-out]" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-panel bg-surface p-6 shadow-2xl">
          <form onSubmit={submit} className="space-y-4">
            <Dialog.Title className="font-heading text-xl font-semibold text-ink">{title}</Dialog.Title>
            <Dialog.Description className="sr-only">Fill in the fields and save. Fields marked with a star are required.</Dialog.Description>
            <ErrorBanner message={error} />
            <FieldErrors.Provider value={fieldErrors}>
              <div className="space-y-3">{children}</div>
            </FieldErrors.Provider>
            <div className="flex justify-end gap-2 pt-2">
              <Button disabled={busy} onClick={onClose}>Cancel</Button>
              <Button type="submit" variant="primary" disabled={busy}>{busy ? 'Saving…' : submitLabel}</Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
