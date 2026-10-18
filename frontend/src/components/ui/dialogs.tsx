import * as Dialog from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { Button } from './Button'

/** Side panel for details and edit forms. Only dialogs and drawers get a shadow. */
export function Drawer({
  open, onOpenChange, title, description, children, width = 'max-w-xl',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  children: ReactNode
  width?: string
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40 data-[state=open]:animate-[fade-in_0.15s_ease-out]" />
        <Dialog.Content
          className={cn('fixed inset-y-0 right-0 z-50 flex w-full flex-col bg-surface shadow-2xl data-[state=open]:animate-[drawer-in_0.2s_ease-out]', width)}
        >
          <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
            <div>
              <Dialog.Title className="font-heading text-xl font-semibold text-ink">{title}</Dialog.Title>
              <Dialog.Description className={description ? 'mt-0.5 text-sm text-ink-muted' : 'sr-only'}>{description ?? title}</Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <Button variant="ghost" size="sm" aria-label="Close">
                <X size={16} aria-hidden="true" />
              </Button>
            </Dialog.Close>
          </header>
          <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

/** "Are you sure?" that restates what will happen. */
export function ConfirmDialog({
  open, onOpenChange, title, description, confirmLabel, tone = 'primary', busy, onConfirm, children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: ReactNode
  confirmLabel: string
  tone?: 'primary' | 'danger'
  busy?: boolean
  onConfirm: () => void
  children?: ReactNode
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40 data-[state=open]:animate-[fade-in_0.15s_ease-out]" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-panel bg-surface p-5 shadow-2xl">
          <Dialog.Title className="font-heading text-xl font-semibold text-ink">{title}</Dialog.Title>
          <Dialog.Description asChild>
            <div className="mt-2 text-sm text-ink">{description}</div>
          </Dialog.Description>
          {children && <div className="mt-3">{children}</div>}
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close asChild>
              <Button disabled={busy}>Cancel</Button>
            </Dialog.Close>
            <Button variant={tone === 'danger' ? 'danger' : 'primary'} disabled={busy} onClick={onConfirm}>
              {busy ? 'Working…' : confirmLabel}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
