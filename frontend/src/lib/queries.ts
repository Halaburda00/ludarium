import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseInfiniteQueryResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { api, ApiError } from '@/lib/api'
import type { components } from '@/lib/api-types'

/**
 * The shapes the API actually publishes, aliased rather than transcribed (#35).
 *
 * Hand-written copies of these matched the backend on the day they were typed
 * and nothing kept them matching: a renamed field passed the backend's contract
 * test, passed a vitest suite whose fixtures were written to the same hand-made
 * shape, and reached the user as an empty column. Named through
 * `docs/openapi.json`, a rename is a compile error at every place that reads the
 * field — including the fixtures.
 */
type Schemas = components['schemas']

export type Account = Schemas['AccountResponse']
export type SyncRun = Schemas['SyncRunResponse']
/** One copy the user owns. The platform column of the table is a list of these. */
export type EntitlementSummary = Schemas['EntitlementSummary']
export type WorkSummary = Schemas['WorkSummary']
export type WorksPage = Schemas['WorksPage']
export type Connection = Schemas['ConnectRequest']
export type Credentials = Schemas['LoginRequest']

export const accountsKey = ['accounts'] as const
export const worksKey = ['works'] as const

/**
 * The connected accounts, and the session probe in the same request.
 *
 * There is no `/api/auth/session` endpoint, and adding one would be a second
 * way to ask the same question: every guarded endpoint answers 401 without a
 * cookie, and this is the one the shell needs the answer to anyway.
 */
export function useAccounts(): UseQueryResult<Account[], ApiError> {
  return useQuery<Account[], ApiError>({
    queryKey: accountsKey,
    queryFn: () => api<Account[]>('/api/accounts'),
    // A 401 is an answer, not a network blip. Retrying it delays the redirect
    // to the login screen for no gain.
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

export function useLogin() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, Credentials>({
    mutationFn: (credentials) =>
      api('/api/auth/login', { method: 'POST', body: credentials }),
    // Everything, unlike its neighbours: before a login every query is sitting
    // on a 401, so there is no key worth sparing.
    onSuccess: () => client.invalidateQueries(),
  })
}

export function useLogout() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, void>({
    mutationFn: () => api('/api/auth/logout', { method: 'POST' }),
    // Cleared rather than invalidated: refetching a library we are no longer
    // allowed to read would answer 401 and flash an error on the way out.
    onSuccess: () => client.clear(),
  })
}

export function useConnect() {
  const client = useQueryClient()
  return useMutation<Account, ApiError, Connection>({
    mutationFn: (connection) => api<Account>('/api/accounts', { method: 'POST', body: connection }),
    onSuccess: () => client.invalidateQueries({ queryKey: accountsKey }),
  })
}

export function useSync() {
  const client = useQueryClient()
  return useMutation<SyncRun[], ApiError, string>({
    mutationFn: (provider) => api<SyncRun[]>(`/api/sync/${provider}`, { method: 'POST' }),
    // Not awaited. `invalidateQueries` resolves only once the refetch is done,
    // and refetching an infinite query replays every loaded page in sequence —
    // each page param comes out of the page before it, so five loaded pages are
    // five round-trips. Awaited, all five sit inside the mutation's `isPending`
    // and the sync button stays disabled long after the sync itself finished.
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: worksKey })
      void client.invalidateQueries({ queryKey: accountsKey })
    },
  })
}

/**
 * The library, a page at a time, following the cursor the API hands back.
 *
 * `useInfiniteQuery` rather than a page number held in state: the backend keys
 * its pages on `(sort_title, id)` and there is no arithmetic that turns "page
 * 3" into that key. Every loaded page stays in one cache entry, so a sync
 * invalidating `worksKey` refetches what the user is actually looking at rather
 * than dropping them back to the top.
 */
export function useWorks(): UseInfiniteQueryResult<InfiniteData<WorksPage>, ApiError> {
  return useInfiniteQuery({
    queryKey: worksKey,
    queryFn: ({ pageParam, signal }) => api<WorksPage>(pageUrl(pageParam), { signal }),
    // Annotated, and the rest inferred. A bare `null` makes TypeScript infer the
    // page param as the type `null`, which then contradicts a
    // `getNextPageParam` returning a string; the five explicit generics this
    // replaces were all working around that one ambiguity.
    initialPageParam: null as Cursor,
    // Null on the last page, which is how TanStack learns there is no more to
    // ask for; returning `undefined` is the same signal and the API never sends
    // it, so the two cases stay one.
    getNextPageParam: (page) => page.next_cursor,
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

type Cursor = string | null

function pageUrl(cursor: Cursor): string {
  // Through `URLSearchParams` rather than by concatenation: the cursor is
  // base64url today and opaque by design, so nothing here should depend on it
  // staying safe to paste into a query string.
  return cursor === null ? '/api/works' : `/api/works?${new URLSearchParams({ cursor })}`
}
