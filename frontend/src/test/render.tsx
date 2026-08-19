import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'

import '@/i18n'

/**
 * A client per test.
 *
 * `retryDelay: 0` rather than `retry: false` everywhere: a hook that sets its
 * own retry policy — `useAccounts` retries a 5xx and never a 401 — keeps it, so
 * the policy stays under test while the waiting does not.
 */
export function renderApp(ui: ReactElement, { route = '/' }: { route?: string } = {}) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, retryDelay: 0 },
      mutations: { retry: false, retryDelay: 0 },
    },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

type Reply = { status?: number; body?: unknown }

/**
 * `fetch`, answering from a table of `METHOD /path` to a reply.
 *
 * A stub rather than a mock server: the shapes are the ones `test_api_contract`
 * already pins on the other side, and a request this table does not name is an
 * error rather than an empty answer — a screen calling something nobody
 * expected should fail loudly.
 */
export function stubFetch(routes: Record<string, Reply>) {
  const calls: { method: string; path: string; body: unknown; credentials?: RequestCredentials }[] =
    []
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    const method = init?.method ?? 'GET'
    calls.push({
      method,
      path,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      credentials: init?.credentials,
    })
    const reply = routes[`${method} ${path}`]
    if (!reply) {
      throw new Error(`no stub for ${method} ${path}`)
    }
    const status = reply.status ?? 200
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fetcher)
  return calls
}
