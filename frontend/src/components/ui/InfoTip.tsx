import * as Tooltip from '@radix-ui/react-tooltip'
import { Info } from 'lucide-react'
import type { ReactNode } from 'react'
import { GLOSSARY, type GlossaryTerm } from '../../lib/glossary'

/** Wrap the app once so every tooltip shares the same open delay. */
export const TooltipProvider = ({ children }: { children: ReactNode }) => (
  <Tooltip.Provider delayDuration={150}>{children}</Tooltip.Provider>
)

/** A small (i) that explains a metric in plain words. Works by hover, focus and keyboard. */
export function InfoTip({ term, text }: { term?: GlossaryTerm; text?: string }) {
  const content = text ?? (term ? GLOSSARY[term] : '')
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button type="button" aria-label="What does this mean?" className="rounded-full text-ink-muted hover:text-primary">
          <Info size={14} aria-hidden="true" />
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          sideOffset={6}
          className="z-[70] max-w-xs rounded-control bg-ink px-3 py-2 text-xs leading-relaxed text-white shadow-lg"
        >
          {content}
          <Tooltip.Arrow className="fill-ink" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  )
}

/** A tooltip around any element, used for disabled buttons ("Available once ranking is built"). */
export function Hint({ text, children }: { text: string; children: ReactNode }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <span tabIndex={0} className="inline-flex">{children}</span>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content sideOffset={6} className="z-[70] max-w-xs rounded-control bg-ink px-3 py-2 text-xs text-white shadow-lg">
          {text}
          <Tooltip.Arrow className="fill-ink" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  )
}
