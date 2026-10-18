import type { ReactNode } from 'react'
import { Drawer as UiDrawer } from './ui'

/** Right-hand slide-over panel (Radix dialog: Escape and the Close button dismiss it, focus stays inside). */
export default function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <UiDrawer open onOpenChange={(open) => !open && onClose()} title={title}>
      <div className="space-y-6">{children}</div>
    </UiDrawer>
  )
}

export function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="font-heading text-xl font-semibold text-ink">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}
