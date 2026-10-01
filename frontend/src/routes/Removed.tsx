import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { ThemePicker } from '@/components/ThemePicker'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import { useRemoved, useRestore, type RemovedEntitlement } from '@/lib/queries'

/**
 * The copies a sync no longer saw, each a click away from coming back (rule 1).
 *
 * A removal is never a deletion, so restoring one brings its work back with
 * the playtime, status and notes it had. A restored copy is kept from then on
 * while its platform stays silent about it (ADR-0030).
 */
export default function Removed() {
  const { t } = useTranslation()
  const removed = useRemoved()
  const restore = useRestore()

  let body: React.ReactNode
  if (removed.isPending) {
    body = <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
  } else if (removed.isError) {
    body = (
      <div className="grid justify-items-start gap-3">
        <Notice>{removed.error.detail || t('error.offline')}</Notice>
        <Button variant="outline" onClick={() => void removed.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    )
  } else if (removed.data.length === 0) {
    body = <p className="text-sm text-muted-foreground">{t('removed.empty')}</p>
  } else {
    body = (
      <Copies
        copies={removed.data}
        // Only the copy being restored waits; the rest stay clickable.
        restoring={restore.isPending ? restore.variables : null}
        onRestore={(id) => restore.mutate(id)}
      />
    )
  }

  return (
    <main className="mx-auto grid max-w-4xl gap-6 px-6 py-10">
      <header className="flex items-center justify-between gap-4">
        <nav>
          <Link to="/library" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('work.back')}
          </Link>
        </nav>
        <ThemePicker />
      </header>
      <div className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t('removed.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('removed.intro')}</p>
      </div>
      {restore.isError ? <Notice>{restore.error.detail}</Notice> : null}
      {body}
    </main>
  )
}

function Copies({
  copies,
  restoring,
  onRestore,
}: {
  copies: RemovedEntitlement[]
  restoring: number | null
  onRestore: (id: number) => void
}) {
  const { t, i18n } = useTranslation()
  const day = new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium' })
  return (
    <table className="w-full border-collapse text-left text-sm">
      <thead>
        <tr className="border-b border-border text-xs text-muted-foreground uppercase">
          <th scope="col" className="py-2 pr-4 font-medium">
            {t('removed.game')}
          </th>
          <th scope="col" className="py-2 pr-4 font-medium">
            {t('work.platform')}
          </th>
          <th scope="col" className="py-2 pr-4 font-medium">
            {t('removed.removedOn')}
          </th>
          <th scope="col" className="py-2">
            <span className="sr-only">{t('removed.actions')}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {copies.map((copy) => (
          <tr key={copy.id} className="border-b border-border/50">
            <th scope="row" className="py-2 pr-4 font-normal">
              {copy.work_title ?? copy.provider_title}
              {/* The store's own name, where it differs: what the user would
                  look for on the platform to check. */}
              {copy.work_title && copy.work_title !== copy.provider_title ? (
                <span className="block text-xs text-muted-foreground">{copy.provider_title}</span>
              ) : null}
            </th>
            <td className="py-2 pr-4">
              {t('removed.platform', { provider: copy.provider_name, account: copy.account_label })}
            </td>
            <td className="py-2 pr-4 tabular-nums">{day.format(new Date(copy.removed_at))}</td>
            <td className="py-2 text-right">
              <Button
                size="sm"
                variant="outline"
                disabled={restoring === copy.id}
                onClick={() => onRestore(copy.id)}
                aria-label={t('removed.restoreLabel', {
                  title: copy.work_title ?? copy.provider_title,
                })}
              >
                {t('removed.restore')}
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
