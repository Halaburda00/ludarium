import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'

import { WorksTable } from '@/components/WorksTable'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import {
  useAccounts,
  useEnrichment,
  useLogout,
  useSync,
  useWorks,
  type SyncOverview,
  type SyncRun,
} from '@/lib/queries'

export default function Library() {
  const { t } = useTranslation()
  const works = useWorks()
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

  return (
    <main className="mx-auto grid max-w-4xl gap-6 px-6 py-10">
      <header className="flex items-baseline justify-between gap-4">
        <h1 className="font-heading text-2xl font-semibold">{t('library.title')}</h1>
        <div className="flex items-center gap-2">
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
        <Notice tone="warn">{t('library.partial', { count: landed })}</Notice>
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
      {works.isError ? (
        <State>
          <Notice>{works.error.detail || t('error.offline')}</Notice>
          <Button variant="outline" onClick={() => void works.refetch()}>
            {t('common.retry')}
          </Button>
        </State>
      ) : null}

      {exhausted && loaded.length === 0 ? (
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

      {works.isSuccess && !(exhausted && loaded.length === 0) ? (
        <>
          <p className="text-sm text-muted-foreground">
            {/* Counted honestly: with a page still unfetched this is what has
                been loaded, not what the library holds, and saying "40 games"
                over the first page of four hundred is simply wrong. */}
            {t('library.count', {
              count: loaded.length,
              context: works.hasNextPage ? 'partial' : undefined,
            })}
          </p>
          <WorksTable works={loaded} />
          {works.hasNextPage ? (
            <Button
              variant="outline"
              className="justify-self-start"
              onClick={() => void works.fetchNextPage()}
              disabled={works.isFetchingNextPage}
            >
              {works.isFetchingNextPage ? t('common.loading') : t('library.loadMore')}
            </Button>
          ) : null}
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
