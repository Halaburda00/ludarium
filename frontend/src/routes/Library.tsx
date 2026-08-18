import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

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
  const landed = runs.reduce((total, run) => total + run.items_seen, 0)

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
      {runs.length > 0 && !failed ? (
        <Notice tone="ok">{t('library.synced', { count: landed })}</Notice>
      ) : null}

      {works.isPending ? <p className="text-sm text-muted-foreground">{t('common.loading')}</p> : null}
      {works.data && works.data.works.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('library.empty')}</p>
      ) : null}
      {works.data && works.data.works.length > 0 ? (
        <>
          <p className="text-sm text-muted-foreground">
            {t('library.count', { count: works.data.works.length })}
          </p>
          {/* The table itself is #13; this is the shell it lands in. */}
          <ul className="grid gap-1">
            {works.data.works.map((work) => (
              <li key={work.id} className="rounded-lg border border-border px-3 py-2 text-sm">
                {work.title}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </main>
  )
}
