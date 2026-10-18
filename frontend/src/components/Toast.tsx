/**
 * Toast API kept as `useToast().success / error / warning` so older pages keep working.
 * The toasts themselves are drawn by sonner (the <Toaster /> in main.tsx).
 */
import { toast } from 'sonner'

interface ToastApi {
  success: (message: string) => void
  error: (message: string) => void
  warning: (message: string) => void
}

const api: ToastApi = {
  success: (m) => void toast.success(m),
  error: (m) => void toast.error(m),
  warning: (m) => void toast.warning(m),
}

export const useToast = (): ToastApi => api
