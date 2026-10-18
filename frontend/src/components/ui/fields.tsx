import { useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react'
import { cn } from '../../lib/cn'

const CONTROL =
  'w-full rounded-control border border-line bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted disabled:bg-canvas disabled:text-ink-muted'

/** Label, control and inline error, wired together for screen readers. */
function Field({
  label, required, error, hint, children,
}: {
  label: string
  required?: boolean
  error?: string
  hint?: string
  children: (props: { id: string; 'aria-invalid': boolean; 'aria-describedby'?: string }) => ReactNode
}) {
  const id = useId()
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-ink">
        {label}
        {required && <span className="ml-0.5 text-risk-high" aria-hidden="true">*</span>}
        {required && <span className="sr-only"> (required)</span>}
      </label>
      {children({ id, 'aria-invalid': Boolean(error), 'aria-describedby': describedBy })}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-risk-high">{error}</p>
      ) : (
        hint && <p id={`${id}-hint`} className="text-xs text-ink-muted">{hint}</p>
      )}
    </div>
  )
}

interface Common {
  label: string
  error?: string
  hint?: string
}

export function TextField({ label, error, hint, required, className, ...rest }: Common & InputHTMLAttributes<HTMLInputElement>) {
  return (
    <Field label={label} error={error} hint={hint} required={required}>
      {(a11y) => <input required={required} className={cn(CONTROL, error && 'border-risk-high', className)} {...a11y} {...rest} />}
    </Field>
  )
}

export function SelectField({ label, error, hint, required, className, children, ...rest }: Common & SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <Field label={label} error={error} hint={hint} required={required}>
      {(a11y) => (
        <select required={required} className={cn(CONTROL, error && 'border-risk-high', className)} {...a11y} {...rest}>
          {children}
        </select>
      )}
    </Field>
  )
}

export function TextAreaField({ label, error, hint, required, className, ...rest }: Common & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <Field label={label} error={error} hint={hint} required={required}>
      {(a11y) => <textarea required={required} rows={3} className={cn(CONTROL, error && 'border-risk-high', className)} {...a11y} {...rest} />}
    </Field>
  )
}
