import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'

import { WorksTable } from '@/components/WorksTable'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import { useLogout, useSync, useWorks } from '@/lib/queries'

export default function Library() {
  const { t } = useTranslation()
  const works = useWorks()
  const sync = useSync()
  const logout = useLogout()
  const navigate = useNavigate()

  const runs = sync.data ?? []
  const failed = runs.find((run) => run.status === 'failed')
  // Its own answer, not a shade of success: a partial run means part of the
  // library did not come through, and reporting "Synced 40 games" over it tells
  // the user everything arrived when it did not.
  const partial = runs.find((run) => run.status === 'partial')
  const landed = runs.reduce((total, run) => total + run.items_seen, 0)

  // Flattened here rather than in the hook: the pages are a transport detail and
  // nothing below this line has a reason to know the library arrived in three
  // requests.
  const loaded = works.data?.pages.flatMap((page) => page.works) ?? []

  return (
    <main className="mx-auto grid max-w-4xl gap-6 px-6 py-10">
      <header className="flex items-baseline justify-between gap-4">
        <h1 className="font-heading text-2xl font-semibold">{t('library.title')}</h1>
        <div className="flex items-center gap-2">
          <Button onClick={() => sync.mutate('steam')} disabled={sync.isPending}>
            {sync.isPending ? t('library.syncing') : t('library.sync')}
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
      {failed ? (
        <Notice>{t('library.runFailed', { reason: failed.error_text ?? t('error.unexpected') })}</Notice>
      ) : null}
      {partial && !failed ? (
        <Notice tone="warn">{t('library.partial', { count: landed })}</Notice>
      ) : null}
      {runs.length > 0 && !failed && !partial ? (
        <Notice tone="ok">{t('library.synced', { count: landed })}</Notice>
      ) : null}

      {works.isPending ? <p className="text-sm text-muted-foreground">{t('common.loading')}</p> : null}

      {/* The library failed to load, which is not the same as it being empty —
          and the difference matters, because one of them is worth retrying. */}
      {works.isError ? (
        <div className="grid justify-items-start gap-3">
          <Notice>{works.error.detail || t('error.offline')}</Notice>
          <Button variant="outline" onClick={() => void works.refetch()}>
            {t('common.retry')}
          </Button>
        </div>
      ) : null}

      {works.isSuccess && loaded.length === 0 ? (
        <div className="grid justify-items-start gap-3">
          <p className="text-sm text-muted-foreground">{t('library.empty')}</p>
          {/* An account is connected — the route guard sends anyone without one
              to onboarding — so the way out of an empty library is a sync, or a
              second account if the first one was the wrong one. */}
          <Link to="/onboarding" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('library.connectAnother')}
          </Link>
        </div>
      ) : null}

      {loaded.length > 0 ? (
        <>
          <p className="text-sm text-muted-foreground">
            {/* Counted honestly: with a page still unfetched this is what has
                been loaded, not what the library holds, and saying "40 games"
                over the first page of four hundred is simply wrong. */}
            {works.hasNextPage
              ? t('library.countSoFar', { count: loaded.length })
              : t('library.count', { count: loaded.length })}
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
