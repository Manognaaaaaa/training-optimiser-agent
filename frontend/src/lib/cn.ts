import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Join class names and let the last Tailwind class win (cn('p-2', cond && 'p-4') gives 'p-4'). */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
