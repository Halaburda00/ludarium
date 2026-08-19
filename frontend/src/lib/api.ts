/**
 * One fetch wrapper, so that "send the session cookie" is decided once.
 *
 * The cookie is httpOnly, which is the point: the frontend cannot read it, and
 * so it cannot be persuaded to store it anywhere either. `credentials:
 * 'include'` is the whole of the auth handling here.
 */

export class ApiError extends Error {
  // Declared and assigned rather than a parameter property: `erasableSyntaxOnly`
  // is on, and a parameter property is syntax that has to be compiled away.
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type Options = {
  method?: string
  body?: unknown
  signal?: AbortSignal
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const response = await fetch(path, {
    method: options.method ?? 'GET',
    credentials: 'include',
    headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })

  if (!response.ok) {
    throw new ApiError(response.status, await detailOf(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

/**
 * FastAPI's `detail`, or the status text.
 *
 * Never the raw body: a 500 from a proxy is an HTML page, and putting that in
 * the UI turns a bad moment into an unreadable one.
 */
async function detailOf(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
      return body.detail
    }
  } catch {
    // Not JSON. The status is all we know.
  }
  return response.statusText
}
