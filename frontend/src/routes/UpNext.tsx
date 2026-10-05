import { ArrowDown, ArrowUp, ArrowUpToLine, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { ThemePicker } from '@/components/ThemePicker'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import {
  queueMove,
  useMoveInQueue,
  useQueue,
  useUpdateState,
  type WorkSummary,
} from '@/lib/queries'

/**
 * The play queue: what the user chose to play next, in their order (#122).
 *
 * Reordered by buttons rather than by dragging. A drag through a long list is
 * unreliable on a phone and unusable from a keyboard, and the buttons cover
 * both without a new dependency.
 */
export default function UpNext() {
  const { t } = useTranslation()
  const queue = useQueue()
  const move = useMoveInQueue()

  let body: React.ReactNode
  if (queue.isPending) {
    body = <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
  } else if (queue.isError) {
    body = (
      <div className="grid justify-items-start gap-3">
        <Notice>{queue.error.detail || t('error.offline')}</Notice>
        <Button variant="outline" onClick={() => void queue.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    )
  } else if (queue.data.length === 0) {
    body = <p className="text-sm text-muted-foreground">{t('queue.empty')}</p>
  } else {
    const works = queue.data
    body = (
      <ol className="grid gap-2">
        {works.map((work, index) => (
          <Entry
            key={work.id}
            work={work}
            place={index + 1}
            last={index === works.length - 1}
            // One move at a time: each is sent with the positions the list
            // held before it, and a second sent alongside would read them stale.
            busy={move.isPending}
            onMove={(to) => move.mutate(queueMove(works, work.id, to))}
          />
        ))}
      </ol>
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
        <h1 className="font-heading text-2xl font-semibold">{t('queue.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('queue.intro')}</p>
      </div>
      {move.isError ? <Notice>{move.error.detail || t('error.offline')}</Notice> : null}
      {body}
    </main>
  )
}

function Entry({
  work,
  place,
  last,
  busy,
  onMove,
}: {
  work: WorkSummary
  place: number
  last: boolean
  busy: boolean
  onMove: (to: number) => void
}) {
  const { t } = useTranslation()
  const leave = useUpdateState(work.id)
  const first = place === 1
  const platforms = [...new Set(work.entitlements.map((copy) => copy.provider_name))]
  const title = work.title
  return (
    <li className="flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2">
      <span className="w-6 text-right text-sm font-semibold tabular-nums text-muted-foreground">
        {place}
      </span>
      <div className="h-12 w-9 shrink-0 overflow-hidden rounded-sm bg-muted">
        {work.cover ? (
          // The title is right beside it.
          <img src={work.cover.url} alt="" className="h-full w-full object-cover" />
        ) : null}
      </div>
      <div className="grid min-w-0 flex-1 gap-0.5">
        <Link
          to={`/library/${work.id}`}
          className="truncate text-sm font-medium underline-offset-4 hover:underline"
        >
          {title}
        </Link>
        <span className="truncate text-xs text-muted-foreground">
          {platforms.join(', ')}
          {platforms.length > 0 ? ' · ' : null}
          {work.playtime_minutes
            ? t('work.playtime', {
                hours: Math.floor(work.playtime_minutes / 60),
                minutes: work.playtime_minutes % 60,
              })
            : t('work.notPlayed')}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          size="icon-sm"
          variant="ghost"
          disabled={busy || first}
          onClick={() => onMove(0)}
          aria-label={t('queue.toTop', { title })}
          title={t('queue.toTop', { title })}
        >
          <ArrowUpToLine />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          disabled={busy || first}
          onClick={() => onMove(place - 2)}
          aria-label={t('queue.up', { title })}
          title={t('queue.up', { title })}
        >
          <ArrowUp />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          disabled={busy || last}
          onClick={() => onMove(place)}
          aria-label={t('queue.down', { title })}
          title={t('queue.down', { title })}
        >
          <ArrowDown />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          disabled={busy || leave.isPending}
          onClick={() => leave.mutate({ play_status: 'not_started' })}
          aria-label={t('queue.remove', { title })}
          title={t('queue.remove', { title })}
        >
          <X />
        </Button>
      </div>
      {leave.isError ? (
        <div className="basis-full">
          <Notice>{leave.error.detail || t('error.offline')}</Notice>
        </div>
      ) : null}
    </li>
  )
}
