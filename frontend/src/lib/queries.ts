import { useEffect, useRef } from 'react'
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useMutationState,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseInfiniteQueryResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { api, ApiError } from '@/lib/api'
import { NO_FILTERS, type Filters } from '@/lib/filters'
import { DEFAULT_SORTING, type Sorting } from '@/lib/sorting'
import { viewQuery } from '@/lib/views'
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
/** A sync's runs, and the steps it queued to run after it. */
export type SyncResult = Schemas['SyncResponse']
/** A platform the sync endpoint turned away rather than ran: 409, already syncing, and the like. */
export type Refusal = { provider: string; status: number; detail: string }
/** Every platform's sync, as one answer: what ran, and what was turned away. */
export type SyncOutcome = SyncResult & { refused: Refusal[] }
export type SyncOverview = Schemas['SyncOverviewResponse']
/** One copy the user owns. The platform column of the table is a list of these. */
export type EntitlementSummary = Schemas['EntitlementSummary']
export type WorkSummary = Schemas['WorkSummary']
/** A Metacritic score with the page it is credited to; never one without the other. */
export type Score = Schemas['Score']
export type SteamReviews = Schemas['SteamReviews']
export type WorksPage = Schemas['WorksPage']
export type WorkDetail = Schemas['WorkDetail']
/** Another work in the library, named well enough to link to: an add-on, or its game. */
export type WorkLink = Schemas['WorkLink']
export type Credit = Schemas['Credit']
/** A genre as IGDB names it; the UI translates it by `slug` where it can. */
export type Genre = Schemas['GenreSummary']
/** A named library query. `dropped` names what the library no longer takes. */
export type SavedView = Schemas['SavedViewResponse']
export type PlayStatus = Schemas['PlayStatus']
export type ItemKind = Schemas['ItemKind']
/**
 * Any subset of what the user decides about a work. A null clears a field; a
 * missing one is kept.
 *
 * `Partial`, because openapi-typescript reads a field with a default as always
 * present — true of a response, false of this request, where leaving a field
 * out is the point.
 */
export type StateUpdate = Partial<Schemas['StateUpdate']>
/** A copy a run no longer saw, offered back in one click (rule 1). */
export type RemovedEntitlement = Schemas['RemovedEntitlement']
/** A copy no platform reports — a disc, a key, an itch.io download — as the user wrote it. */
export type ManualEntry = Schemas['ManualEntryResponse']
/**
 * What the form sends: the whole entry every time. `Required`, because a
 * field with a default reads as optional, and the form always says each one.
 */
export type ManualEntryForm = Required<Schemas['ManualEntry']>
export type OwnershipType = Schemas['OwnershipType']
export type Connection = Schemas['ConnectRequest']
export type Credentials = Schemas['LoginRequest']
export type Health = Schemas['HealthResponse']

export const accountsKey = ['accounts'] as const
export const worksKey = ['works'] as const
export const syncOverviewKey = ['sync', 'runs'] as const
export const viewsKey = ['views'] as const

/**
 * How often the library asks whether the steps after a sync are done. A step
 * takes seconds to minutes, and the answer is one small read.
 */
export const ENRICHMENT_POLL_MS = 2000

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

/**
 * Whether the instance is a demo (ADR-0034). Public, so it answers before a
 * session does, and asked once: an instance does not become a demo mid-visit.
 */
export function useHealth(): UseQueryResult<Health, ApiError> {
  return useQuery<Health, ApiError>({
    queryKey: ['health'],
    queryFn: () => api<Health>('/api/health'),
    staleTime: Infinity,
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
  return useMutation<SyncOutcome, ApiError, string[]>({
    mutationFn: syncEach,
    // Not awaited. `invalidateQueries` resolves only once the refetch is done,
    // and refetching an infinite query replays every loaded page in sequence —
    // each page param comes out of the page before it, so five loaded pages are
    // five round-trips. Awaited, all five sit inside the mutation's `isPending`
    // and the sync button stays disabled long after the sync itself finished.
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: worksKey })
      void client.invalidateQueries({ queryKey: accountsKey })
      // Written in at once, then confirmed. Until the refetch answers, the
      // cached overview says nothing is running, and a sync button freed in
      // that gap starts a second sync whose answer replaces this one's — and
      // with it the record of which steps this one queued.
      client.setQueryData<SyncOverview>(syncOverviewKey, (overview) =>
        overview
          ? {
              ...overview,
              enriching: [...new Set([...overview.enriching, ...result.enriching])],
            }
          : overview,
      )
      void client.invalidateQueries({ queryKey: syncOverviewKey })
    },
  })
}

/**
 * Every platform in turn, as one answer.
 *
 * One at a time rather than at once: each sync writes the library, and the
 * database takes one writer. A platform whose provider fails does so as a
 * failed run (rule 4). One the endpoint turns away — a sync already running
 * on it — does not stop the others either, and is kept by name in `refused`,
 * so a screen can say which platform did not run rather than report the rest
 * as the whole. Raised only when every platform was turned away.
 */
async function syncEach(providers: string[]): Promise<SyncOutcome> {
  const merged: SyncOutcome = { runs: [], enriching: [], refused: [] }
  let first: ApiError | null = null
  for (const provider of providers) {
    try {
      const result = await api<SyncResult>(`/api/sync/${provider}`, { method: 'POST' })
      merged.runs.push(...result.runs)
      merged.enriching = [...new Set([...merged.enriching, ...result.enriching])]
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      first ??= error
      merged.refused.push({ provider, status: error.status, detail: error.detail })
    }
  }
  if (first && merged.refused.length === providers.length) throw first
  return merged
}

/**
 * The steps a sync queued that have not finished — classification, matching,
 * scores — polled while there are any, and the library refetched once there
 * are none (#70).
 *
 * They run after the sync has answered, so the library the sync refetched is
 * the one from before them. Without this the scores of a two-minute RAWG run
 * appear only when something else happens to reload the page.
 *
 * Asked on mount too, so a page opened while they run says so as well.
 */
export function useEnrichment(): UseQueryResult<SyncOverview, ApiError> {
  const client = useQueryClient()
  const overview = useQuery<SyncOverview, ApiError>({
    queryKey: syncOverviewKey,
    queryFn: () => api<SyncOverview>('/api/sync/runs'),
    refetchInterval: (query) => (query.state.data?.enriching.length ? ENRICHMENT_POLL_MS : false),
    // A progress line, not the library: a failed poll is simply no line.
    retry: false,
  })
  const running = (overview.data?.enriching.length ?? 0) > 0
  // Whether this page saw anything running, so that "nothing running" on the
  // first answer is not mistaken for something having just finished.
  const watched = useRef(false)
  useEffect(() => {
    if (running) {
      watched.current = true
    } else if (watched.current) {
      watched.current = false
      void client.invalidateQueries({ queryKey: worksKey })
    }
  }, [running, client])
  return overview
}

/**
 * The library, a page at a time, following the cursor the API hands back.
 *
 * `useInfiniteQuery` rather than a page number held in state: the backend keys
 * its pages on the order's value, a folded title and an id, and there is no arithmetic that turns
 * "page 3" into that key. Every loaded page stays in one cache entry, so a sync
 * invalidating `worksKey` refetches what the user is actually looking at rather
 * than dropping them back to the top.
 */
export function useWorks(
  search = '',
  filters: Filters = NO_FILTERS,
  sorting: Sorting = DEFAULT_SORTING,
): UseInfiniteQueryResult<InfiniteData<WorksPage>, ApiError> {
  // Keyed on the query string the API will be sent, so two filter states that
  // ask the same question share a cache entry. The order is in it too: a
  // cursor is a position in one order, and the API refuses it in another.
  const asked = viewQuery(filters, sorting)
  return useInfiniteQuery({
    // Under `worksKey`, so a sync invalidating the library refetches a search
    // too.
    queryKey: [...worksKey, search, asked],
    queryFn: ({ pageParam, signal }) => api<WorksPage>(pageUrl(pageParam, search, asked), { signal }),
    // The last answer stays on screen while the next search is asked, rather
    // than the grid giving way to "Loading…" on every letter typed.
    placeholderData: keepPreviousData,
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

function pageUrl(cursor: Cursor, search: string, filters: string): string {
  // Through `URLSearchParams` rather than by concatenation: the cursor is
  // base64url today and opaque by design, and a search is whatever was typed,
  // so nothing here should depend on either being safe to paste in.
  const params = new URLSearchParams(filters)
  if (search) params.set('q', search)
  if (cursor !== null) params.set('cursor', cursor)
  const query = params.toString()
  return query ? `/api/works?${query}` : '/api/works'
}

/**
 * One work's page. Under `worksKey`, so a sync or a finished enrichment step
 * refreshes it as it refreshes the grid.
 *
 * A 404 is an answer, not an outage: the work is not in the library, and
 * asking again will not put it there.
 */
export function useWork(id: number, enabled = true): UseQueryResult<WorkDetail, ApiError> {
  return useQuery<WorkDetail, ApiError>({
    enabled,
    queryKey: [...worksKey, 'detail', id],
    queryFn: ({ signal }) => api<WorkDetail>(`/api/works/${id}`, { signal }),
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

/**
 * Change what the user decides about a work.
 *
 * The answer is the work as it now is, so it replaces the cached detail
 * outright rather than waiting for a refetch. The grid is invalidated as well:
 * a status or a hidden flag changes what the listing shows.
 */
export function useUpdateState(id: number) {
  const client = useQueryClient()
  return useMutation<WorkDetail, ApiError, StateUpdate>({
    mutationFn: (update) =>
      api<WorkDetail>(`/api/works/${id}/state`, { method: 'PATCH', body: update }),
    onSuccess: (work) => {
      client.setQueryData([...worksKey, 'detail', id], work)
      void client.invalidateQueries({ queryKey: worksKey, exact: false, refetchType: 'none' })
      // Refetched where it is on screen: the queue view and a work page's
      // place in it number what they show, and a status just changed that.
      void client.invalidateQueries({ queryKey: queueKey })
    },
  })
}

/**
 * The listing as the play queue reads it (ADR-0033): every queued work in its
 * order, hidden ones and add-ons included, since queueing one is a choice the
 * user made about that work itself.
 */
export const QUEUE_QUERY =
  'status=queued&sort=queue&order=asc&hidden=include&addons=separate&limit=500'
export const queueKey = [...worksKey, 'queue'] as const

/**
 * The queue, whole. A queue is a short list, so every page is fetched before
 * it is shown: numbering it from a first page would number it wrong.
 *
 * A work whose every copy was removed keeps its place but is not listed, so
 * the numbers on screen are the order of what is shown, not the stored
 * positions.
 */
export function useQueue(enabled = true): UseQueryResult<WorkSummary[], ApiError> {
  return useQuery<WorkSummary[], ApiError>({
    enabled,
    queryKey: queueKey,
    queryFn: async ({ signal }) => {
      const works: WorkSummary[] = []
      let cursor: Cursor = null
      do {
        const params = new URLSearchParams(QUEUE_QUERY)
        if (cursor !== null) params.set('cursor', cursor)
        const page: WorksPage = await api<WorksPage>(`/api/works?${params}`, { signal })
        works.push(...page.works)
        cursor = page.next_cursor
      } while (cursor !== null)
      return works
    },
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

/**
 * A move within the queue: the work, the index in the shown list it goes to,
 * and the position to send for it.
 *
 * The position is the one the work now at that index holds, read before the
 * move is shown. A work with no live copy keeps a place nobody sees, so an
 * index sent as a position would land beside it rather than where the user
 * put the work.
 */
export type QueueMove = { id: number; to: number; position: number }

/** Where moving `id` to index `to` of the shown queue sends it. */
export function queueMove(works: WorkSummary[], id: number, to: number): QueueMove {
  return { id, to, position: works[to]?.queue_position ?? to + 1 }
}

/**
 * `works` with the one at `id` moved to index `to`, renumbered as the server
 * will: the positions the list held, handed out again in the new order.
 */
export function movedInQueue(works: WorkSummary[], id: number, to: number): WorkSummary[] {
  const from = works.findIndex((work) => work.id === id)
  if (from === -1) return works
  const order = [...works]
  const [work] = order.splice(from, 1)
  order.splice(Math.min(Math.max(to, 0), order.length), 0, work)
  const positions = works.map((work) => work.queue_position ?? 0)
  return order.map((work, index) => ({ ...work, queue_position: positions[index] }))
}

/** Move a queued work, shown at once and put back if the server refuses. */
export function useMoveInQueue() {
  const client = useQueryClient()
  return useMutation<WorkDetail, ApiError, QueueMove, { previous?: WorkSummary[] }>({
    mutationFn: ({ id, position }) =>
      api<WorkDetail>(`/api/works/${id}/queue`, { method: 'PUT', body: { position } }),
    onMutate: async ({ id, to }) => {
      await client.cancelQueries({ queryKey: queueKey })
      const previous = client.getQueryData<WorkSummary[]>(queueKey)
      if (previous) client.setQueryData(queueKey, movedInQueue(previous, id, to))
      return { previous }
    },
    onError: (_error, _move, context) => {
      if (context?.previous) client.setQueryData(queueKey, context.previous)
    },
    onSuccess: (work) => client.setQueryData([...worksKey, 'detail', work.id], work),
    // Awaited for the queue: the positions shown after a move are the ones
    // guessed for it, and the next move must not be sent from them. Every
    // other listing only goes stale, since a move changes the queue order the
    // library can sort by.
    onSettled: async () => {
      void client.invalidateQueries({ queryKey: worksKey, refetchType: 'none' })
      await client.invalidateQueries({ queryKey: queueKey })
    },
  })
}

export function useViews(): UseQueryResult<SavedView[], ApiError> {
  return useQuery<SavedView[], ApiError>({
    queryKey: viewsKey,
    queryFn: ({ signal }) => api<SavedView[]>('/api/views', { signal }),
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

/**
 * Every change to the saved views, one mutation each. Each answers with what
 * it changed, and the list is refetched rather than patched by hand: it is a
 * handful of rows, and a reorder moves all of them.
 */
function useViewMutation<Variables, Result>(request: (variables: Variables) => Promise<Result>) {
  const client = useQueryClient()
  return useMutation<Result, ApiError, Variables>({
    mutationFn: request,
    onSuccess: () => client.invalidateQueries({ queryKey: viewsKey }),
  })
}

export function useSaveView() {
  return useViewMutation((view: { name: string; query: string }) =>
    api<SavedView>('/api/views', { method: 'POST', body: view }),
  )
}

export function useRenameView() {
  return useViewMutation(({ id, name }: { id: number; name: string }) =>
    api<SavedView>(`/api/views/${id}`, { method: 'PATCH', body: { name } }),
  )
}

export function useReorderViews() {
  return useViewMutation((ids: number[]) =>
    api<SavedView[]>('/api/views/order', { method: 'PUT', body: { ids } }),
  )
}

export function useDeleteView() {
  return useViewMutation((id: number) => api<void>(`/api/views/${id}`, { method: 'DELETE' }))
}

/**
 * The genres in the library, for the filter panel. Under `worksKey`, so a sync
 * or a finished IGDB step that brings new genres refreshes them with the grid.
 */
export function useGenres(): UseQueryResult<Genre[], ApiError> {
  return useQuery<Genre[], ApiError>({
    queryKey: [...worksKey, 'genres'],
    queryFn: ({ signal }) => api<Genre[]>('/api/genres', { signal }),
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

/**
 * The copies a successful run stopped seeing (#96).
 *
 * Under `worksKey`, so that a sync, which may remove or bring back a copy,
 * refreshes this list as it refreshes the grid.
 */
export function useRemoved(): UseQueryResult<RemovedEntitlement[], ApiError> {
  return useQuery<RemovedEntitlement[], ApiError>({
    queryKey: [...worksKey, 'removed'],
    queryFn: ({ signal }) => api<RemovedEntitlement[]>('/api/entitlements/removed', { signal }),
  })
}

export const restoreKey = ['entitlements', 'restore'] as const

/**
 * Every copy a restore is under way for. `variables` on the mutation names
 * only the latest, and a second click on an earlier row would send it twice.
 */
export function useRestoring(): number[] {
  return useMutationState({
    filters: { mutationKey: restoreKey, status: 'pending' },
    select: (mutation) => mutation.state.variables as number,
  })
}

/**
 * Put a removed copy back. It is kept from then on, while its platform stays
 * silent about it, until the user lets it go (ADR-0030).
 */
export function useRestore() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, number>({
    mutationKey: restoreKey,
    mutationFn: (id) => api(`/api/entitlements/${id}/restore`, { method: 'POST' }),
    // Asked again whatever the answer: restored, the copy leaves this list and
    // its work comes back to the grid; refused, the list was stale — a sync
    // brought the copy back meanwhile — and only a fresh one says so.
    onSettled: () => client.invalidateQueries({ queryKey: worksKey }),
  })
}

/** Stop keeping a restored copy: the next sync that does not list it removes it. */
export function useLetGo() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, number>({
    mutationFn: (id) => api(`/api/entitlements/${id}/keep`, { method: 'DELETE' }),
    // As `useRestore`: a refusal means the page was out of date.
    onSettled: () => client.invalidateQueries({ queryKey: worksKey }),
  })
}

/** One manual entry as the user last wrote it, for the form that edits it (#97). */
export function useManualEntry(id: number, enabled = true): UseQueryResult<ManualEntry, ApiError> {
  return useQuery<ManualEntry, ApiError>({
    enabled,
    queryKey: [...worksKey, 'manual', id],
    queryFn: ({ signal }) => api<ManualEntry>(`/api/entitlements/manual/${id}`, { signal }),
    retry: (failureCount, error) => error.status >= 500 && failureCount < 2,
  })
}

/**
 * Add a manual entry, or replace one when `id` is given. Either way the
 * library changes, so the grid and every work page are asked again.
 */
export function useSaveManualEntry(id: number | null) {
  const client = useQueryClient()
  return useMutation<ManualEntry, ApiError, ManualEntryForm>({
    mutationFn: (entry) =>
      id === null
        ? api<ManualEntry>('/api/entitlements/manual', { method: 'POST', body: entry })
        : api<ManualEntry>(`/api/entitlements/manual/${id}`, { method: 'PUT', body: entry }),
    onSuccess: () => client.invalidateQueries({ queryKey: worksKey }),
  })
}

/** Delete a manual entry for good: there is no removed view for what the user typed (ADR-0031). */
export function useDeleteManualEntry() {
  const client = useQueryClient()
  return useMutation<unknown, ApiError, number>({
    mutationFn: (id) => api(`/api/entitlements/manual/${id}`, { method: 'DELETE' }),
    // Not refetched: the entry's own query would ask for a row that is gone
    // and answer 404 under the page that is leaving it.
    onSuccess: () => client.invalidateQueries({ queryKey: worksKey, refetchType: 'none' }),
  })
}
