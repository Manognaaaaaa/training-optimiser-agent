const BASE_URL: string = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api'

/** Shape returned by GET /health. */
export interface HealthResponse {
  status: string
  database: string
  tables: number
}

/** Thrown for any non-2xx response. `message` is the backend's readable `detail` string. */
export class ApiError extends Error {
  status: number
  /** {field: message} from a 422, so forms can mark the bad inputs. */
  fieldErrors: Record<string, string>
  /** The whole JSON body of the error response (for example the checker's violations on a 409). */
  body: Record<string, unknown>

  constructor(status: number, detail: string, fieldErrors: Record<string, string> = {}, body: Record<string, unknown> = {}) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
    this.body = body
  }
}

/** Small typed wrapper around fetch. Throws an ApiError carrying the backend's `detail` if not 2xx. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the API. Is the backend running on port 8000?')
  }
  if (!response.ok) {
    let detail = `API error ${response.status} on ${path}`
    let fieldErrors: Record<string, string> = {}
    let errorBody: Record<string, unknown> = {}
    try {
      const body = await response.json()
      if (body && typeof body === 'object') errorBody = body
      if (typeof body.detail === 'string') detail = body.detail
      if (body.errors && typeof body.errors === 'object') fieldErrors = body.errors
    } catch {
      // body was not JSON: keep the generic message
    }
    throw new ApiError(response.status, detail, fieldErrors, errorBody)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const getHealth = () => apiFetch<HealthResponse>('/health')

/** Build '?a=1&b=2' from an object, skipping empty values. */
export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
})

/** Page object returned by every list endpoint. */
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** Paging + sorting params shared by every list. */
export interface ListParams {
  page?: number
  page_size?: number
  sort?: string
}
