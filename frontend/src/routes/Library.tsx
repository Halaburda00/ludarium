import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { FilterPanel } from '@/components/FilterPanel'
import { SavedViews } from '@/components/SavedViews'
import { SortControl } from '@/components/SortControl'
import { ThemePicker } from '@/components/ThemePicker'
import { WorksGrid } from '@/components/WorksGrid'
import { Button } from '@/components/ui/button'
import { Field, Notice } from '@/components/ui/field'
import { activeCount, NO_FILTERS, readFilters, writeFilters, type Filters } from '@/lib/filters'
import { readSorting, writeSorting, type Sorting } from '@/lib/sorting'
import { viewQuery } from '@/lib/views'
import {
  useAccounts,
  useEnrichment,
  useGenres,
  useLogout,
  useSync,
  useWorks,
  type SyncOverview,
  type SyncRun,
} from '@/lib/queries'

/** How long typing has to pause before the library is asked: a request per word, not per letter. */
export const SEARCH_DEBOUNCE_MS = 250

export default function Library() {
  const { t } = useTranslation()
  // In the address, so a search survives a reload and the back button.
  const [params, setParams] = useSearchParams()
  const search = params.get('q') ?? ''
  const [typed, setTyped] = useState(search)
  // Taken from the address when it moved without the field: back and forward,
  // now that filter changes are steps in the history. Left alone, the field
  // would keep the old text and the effect below would write it back over the
  // entry the user just returned to.
  const [seenSearch, setSeenSearch] = useState(search)
  if (search !== seenSearch) {
    setSeenSearch(search)
    if (search !== typed.trim()) setTyped(search)
  }
  useEffect(() => {
    const timer = setTimeout(() => {
      if (typed.trim() === search) return
      setParams(
        (current) => {
          const next = new URLSearchParams(current)
          if (typed.trim()) next.set('q', typed.trim())
          else next.delete('q')
          return next
        },
        { replace: true },
      )
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [typed, search, setParams])
  // In the address with the search, for the same reasons, and pushed rather
  // than replaced: back and forward step through filter changes.
  const filters = readFilters(params)
  const filtered = activeCount(filters) > 0
  const setFilters = (next: Filters) => setParams((current) => writeFilters(next, current))
  // Beside the filters in the address and in the history, and not one of them.
  const sorting = readSorting(params)
  const setSorting = (next: Sorting) => setParams((current) => writeSorting(next, current))
  const works = useWorks(search, filters, sorting)
  const sync = useSync()
  const overview = useEnrichment()
  const accounts = useAccounts()
  // Every platform with an account to sync, once each: the endpoint syncs all
  // of a platform's accounts.
  const platforms = [
    ...new Set(
      (accounts.data ?? [])
        .filter((account) => account.is_active)
        .map((account) => account.provider),
    ),
  ].filter((provider) => SYNCABLE.has(provider))
  // Every platform a copy can be on here, which is wider than what can be
  // synced: a deactivated account's copies are still listed, and a manual
  // entry is a platform with no sync at all. Plus whatever the address names,
  // so a filter from a shared link can be unticked on its own.
  const offered = [
    ...new Set([
      ...(accounts.data ?? []).map((account) => account.provider),
      ...filters.platform,
    ]),
  ]
  // Every genre in the library, plus any the address names that is not, so a
  // filter from an older link can be unticked; its slug is the only name it has.
  const genres = useGenres()
  const offeredGenres = [
    ...(genres.data ?? []),
    ...filters.genre
      .filter((slug) => !(genres.data ?? []).some((genre) => genre.slug === slug))
      .map((slug) => ({ slug, name: slug })),
  ]
  const logout = useLogout()
  const navigate = useNavigate()

  const runs = sync.data?.runs ?? []
  const failed = runs.filter((run) => run.status === 'failed')
  const refused = sync.data?.refused ?? []
  // Its own answer, not a shade of success: a partial run means part of the
  // library did not come through, and reporting "Synced 40 games" over it tells
  // the user everything arrived when it did not.
  const partial = runs.find((run) => run.status === 'partial')
  const landed = runs.reduce((total, run) => total + run.items_seen, 0)
  // Said with the count, so "the platform did not hand over all of it" has a
  // size: one row Steam sent and we could not read is not an outage (#44).
  const unreadable = runs.reduce((total, run) => total + run.items_skipped, 0)

  const enriching = overview.data?.enriching ?? []
  const step = enriching.length > 0 ? providerName(overview.data, enriching[0]) : null
  const stepsFailed = failedSteps(overview.data, sync.data?.enriching ?? [], runs)

  // Flattened here rather than in the hook: the pages are a transport detail and
  // nothing below this line has a reason to know the library arrived in three
  // requests.
  const loaded = works.data?.pages.flatMap((page) => page.works) ?? []
  // Nothing loaded is not the same question as nothing to load. `works.py`
  // deliberately answers `{works: [], next_cursor: "..."}` when a torn read
  // drops every row of a page, and takes the cursor from the last row *read*
  // precisely so the client can keep going. Reading that as an empty library
  // strands the user on "connect an account" with the rest of it unread and no
  // control on the screen that would fetch it.
  const exhausted = works.isSuccess && !works.hasNextPage
  // A later page failing puts the whole query in error, with every page before
  // it still in hand. That is the grid with a retry at its end, not a library
  // that failed to load: the second one hides everything the user was reading.
  const shown = works.data !== undefined
  const { fetchNextPage } = works
  // Stable, so the grid's "ask for the next page" effect runs when the grid
  // wants a page, not whenever this component renders.
  const loadMore = useCallback(() => void fetchNextPage(), [fetchNextPage])

  return (
    <main className="mx-auto grid max-w-6xl gap-6 px-6 py-10">
      <header className="flex items-baseline justify-between gap-4">
        <h1 className="font-heading text-2xl font-semibold">{t('library.title')}</h1>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <ThemePicker />
          {/* Not offered while the steps after a sync are running: another
              sync would skip every step already underway, and the scores the
              user is waiting for arrive on their own when they finish. */}
          <Button
            onClick={() => sync.mutate(platforms)}
            disabled={sync.isPending || step !== null || platforms.length === 0}
          >
            {sync.isPending
              ? t('library.syncing')
              : step !== null
                ? t('library.updating')
                : t('library.sync')}
          </Button>
          {/* Always here, not only over an empty library: a second platform
              is connected from a library that already has the first. */}
          <Link
            to="/onboarding"
            className="px-2 text-sm text-primary underline-offset-4 hover:underline"
          >
            {t('library.connectAccount')}
          </Link>
          <Link
            to="/removed"
            className="px-2 text-sm text-primary underline-offset-4 hover:underline"
          >
            {t('library.removedLink')}
          </Link>
          <Button
            variant="ghost"
            onClick={() =>
              // Sent away, not just emptied. Left here the screen would refetch
              // a library it is no longer allowed to read and answer its own
              // 401 with an error the user has already asked for.
              logout.mutate(undefined, {
                onSuccess: () => void navigate('/login', { replace: true }),
              })
            }
          >
            {t('common.signOut')}
          </Button>
        </div>
      </header>

      {/* 409 is not a failure worth an alarm: something is already doing the
          thing that was asked for. */}
      {sync.isError ? (
        <Notice>
          {sync.error.status === 409 ? t('library.alreadyRunning') : sync.error.detail}
        </Notice>
      ) : null}
      {failed.map((run) => (
        <Notice key={run.id}>
          {t('library.runFailed', {
            provider: providerName(overview.data, run.provider),
            reason: run.error_text ?? t('error.unexpected'),
          })}{' '}
          {/* The user's to fix, so the way to fix it is here. An outage gets
              no link: signing in again would not help, and would suggest it. */}
          {run.error_kind === 'credentials' ? (
            <Link
              to={`/onboarding?provider=${run.provider}`}
              className="underline underline-offset-4"
            >
              {t('library.signInAgain')}
            </Link>
          ) : null}
        </Notice>
      ))}
      {refused.map((refusal) => (
        <Notice key={refusal.provider} tone={refusal.status === 409 ? 'warn' : 'error'}>
          {refusal.status === 409
            ? t('library.platformBusy', { provider: providerName(overview.data, refusal.provider) })
            : t('library.runFailed', {
                provider: providerName(overview.data, refusal.provider),
                reason: refusal.detail,
              })}
        </Notice>
      ))}
      {partial && failed.length === 0 ? (
        <Notice tone="warn">
          {t('library.partial', { count: landed })}
          {unreadable > 0 ? ` ${t('library.unreadable', { count: unreadable })}` : null}
        </Notice>
      ) : null}
      {runs.length > 0 && failed.length === 0 && refused.length === 0 && !partial ? (
        <Notice tone="ok">{t('library.synced', { count: landed })}</Notice>
      ) : null}
      {step !== null ? <Notice tone="ok">{t('library.enriching', { step })}</Notice> : null}
      {stepsFailed.map((run) => (
        <Notice key={run.id} tone="warn">
          {t('library.stepFailed', {
            step: providerName(overview.data, run.provider),
            reason: run.error_text ?? t('error.unexpected'),
          })}
        </Notice>
      ))}

      {works.isPending ? <p className="text-sm text-muted-foreground">{t('common.loading')}</p> : null}

      {/* The library failed to load, which is not the same as it being empty —
          and the difference matters, because one of them is worth retrying. */}
      {works.isError && !shown ? (
        <State>
          <Notice>{works.error.detail || t('error.offline')}</Notice>
          <Button variant="outline" onClick={() => void works.refetch()}>
            {t('common.retry')}
          </Button>
          {/* A link from an older version can name a filter value the API no
              longer takes; retrying it would fail the same way. */}
          {filtered ? (
            <Button variant="outline" onClick={() => setFilters(NO_FILTERS)}>
              {t('filters.clear')}
            </Button>
          ) : null}
        </State>
      ) : null}

      {/* Not over a library with nothing in it: there is nothing to find, and
          the way out of that screen is a sync. */}
      {search || filtered || !(exhausted && loaded.length === 0) ? (
        <search>
          <Field
          id="library-search"
          type="search"
          label={t('library.search')}
          placeholder={t('library.searchHint')}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          autoComplete="off"
          className="max-w-md"
          />
        </search>
      ) : null}

      {/* A view replaces the whole address, search included: it is a named
          link, and a link opens as it was saved. */}
      <SavedViews
        current={viewQuery(filters, sorting)}
        onOpen={(query) => setParams(new URLSearchParams(query))}
      />

      {/* Always here, over an empty grid too: a library where every game is
          hidden looks exactly like one with nothing in it. */}
      <FilterPanel
        filters={filters}
        platforms={offered.map((key) => ({ key, name: providerName(overview.data, key) }))}
        genres={offeredGenres}
        onChange={setFilters}
      />

      {exhausted && loaded.length === 0 && search ? (
        <p className="text-sm text-muted-foreground">{t('library.noMatches', { search })}</p>
      ) : null}

      {exhausted && loaded.length === 0 && !search && filtered ? (
        <State>
          <p className="text-sm text-muted-foreground">{t('filters.noMatches')}</p>
          <Button variant="outline" onClick={() => setFilters(NO_FILTERS)}>
            {t('filters.clear')}
          </Button>
        </State>
      ) : null}

      {exhausted && loaded.length === 0 && !search && !filtered ? (
        <State>
          <p className="text-sm text-muted-foreground">{t('library.empty')}</p>
          {/* An account is connected — the route guard sends anyone without one
              to onboarding — so the way out of an empty library is a sync, or a
              second account if the first one was the wrong one. */}
          <Link to="/onboarding" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('library.connectAnother')}
          </Link>
        </State>
      ) : null}

      {shown && !(exhausted && loaded.length === 0) ? (
        <>
          {/* Only over something to order: an empty grid has no order to change. */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              {/* Counted honestly: with a page still unfetched this is what has
                  been loaded, not what the library holds, and saying "40 games"
                  over the first page of four hundred is simply wrong. */}
              {t(search || filtered ? 'library.matches' : 'library.count', {
                count: loaded.length,
                context: works.hasNextPage ? 'partial' : undefined,
              })}
            </p>
            <SortControl sorting={sorting} onChange={setSorting} />
          </div>
          <WorksGrid
            works={loaded}
            hasMore={works.hasNextPage}
            loadingMore={works.isFetchingNextPage}
            loadFailed={works.isFetchNextPageError}
            onLoadMore={loadMore}
          />
        </>
      ) : null}
    </main>
  )
}

/** The platforms `POST /api/sync/{provider}` can sync. */
const SYNCABLE = new Set(['steam', 'epic'])

function providerName(overview: SyncOverview | undefined, key: string): string {
  return overview?.providers.find((provider) => provider.key === key)?.display_name ?? key
}

/**
 * The steps this page's sync queued that have finished and failed.
 *
 * Each step's latest run, taken only if it began after the sync ended: a step
 * skipped because it was already running opens no run of its own, and the
 * failure of an older run is not news about this sync.
 */
function failedSteps(
  overview: SyncOverview | undefined,
  queued: string[],
  runs: SyncRun[],
): SyncRun[] {
  const synced = runs.map((run) => run.finished_at).filter((at) => at !== null)
  if (!overview || synced.length === 0) {
    return []
  }
  const since = synced.reduce((latest, at) => (at > latest ? at : latest))
  return queued
    .filter((key) => !overview.enriching.includes(key))
    .map((key) => overview.runs.find((run) => run.provider === key))
    .filter(
      (run): run is SyncRun =>
        run !== undefined && run.started_at >= since && run.status === 'failed',
    )
}

/** A message with the control that answers it, which is the shape of both. */
function State({ children }: { children: React.ReactNode }) {
  return <div className="grid justify-items-start gap-3">{children}</div>
}
