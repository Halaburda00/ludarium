import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import { STATUSES } from '@/lib/filters'
import { useUpdateState, type PlayStatus, type WorkDetail } from '@/lib/queries'

const RATINGS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

const CONTROL =
  'h-8 rounded-lg border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50'

/**
 * What the user decides about a work: status, rating, favourite, hidden, notes.
 *
 * Every control but the notes saves as soon as it changes, because each is one
 * choice and there is nothing to confirm. The notes are typed, so they save on
 * a button rather than on every key.
 */
export function WorkState({ work }: { work: WorkDetail }) {
  const { t, i18n } = useTranslation()
  const update = useUpdateState(work.id)
  const [notes, setNotes] = useState(work.notes ?? '')
  const saving = update.isPending
  const notesChanged = notes !== (work.notes ?? '')

  return (
    <section aria-labelledby="work-state" className="grid gap-4">
      <h2 id="work-state" className="font-heading text-lg font-semibold">
        {t('work.state.heading')}
      </h2>
      {update.isError ? <Notice>{update.error.detail || t('error.offline')}</Notice> : null}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
        <label className="flex items-center gap-2">
          {t('work.state.status')}
          <select
            value={work.play_status}
            disabled={saving}
            onChange={(event) => update.mutate({ play_status: event.target.value as PlayStatus })}
            className={CONTROL}
          >
            {STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`playStatus.${status}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2">
          {t('work.state.rating')}
          <select
            value={work.rating ?? ''}
            disabled={saving}
            onChange={(event) =>
              update.mutate({ rating: event.target.value ? Number(event.target.value) : null })
            }
            className={CONTROL}
          >
            <option value="">{t('work.state.noRating')}</option>
            {RATINGS.map((rating) => (
              <option key={rating} value={rating}>
                {rating}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={work.is_favourite}
            disabled={saving}
            onChange={(event) => update.mutate({ is_favourite: event.target.checked })}
          />
          {t('work.state.favourite')}
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={work.is_hidden}
            disabled={saving}
            aria-describedby="work-hidden-hint"
            onChange={(event) => update.mutate({ is_hidden: event.target.checked })}
          />
          {t('work.state.hidden')}
        </label>
      </div>
      <p id="work-hidden-hint" className="text-xs text-muted-foreground">
        {t('work.state.hiddenHint')}
      </p>
      <Dates work={work} language={i18n.language} />
      <div className="grid gap-2">
        <label htmlFor="work-notes" className="text-sm font-medium">
          {t('work.state.notes')}
        </label>
        <textarea
          id="work-notes"
          value={notes}
          maxLength={10000}
          rows={4}
          onChange={(event) => setNotes(event.target.value)}
          className="rounded-lg border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
        />
        <div>
          <Button
            variant="outline"
            disabled={saving || !notesChanged}
            onClick={() =>
              update.mutate(
                { notes: notes || null },
                // The server's copy, not the draft: it stores blank notes as
                // none, and a draft left at "   " would never read as saved.
                { onSuccess: (saved) => setNotes(saved.notes ?? '') },
              )
            }
          >
            {t('work.state.saveNotes')}
          </Button>
        </div>
      </div>
    </section>
  )
}

/** When the user started and finished it, where they have. Set by the server, never typed. */
function Dates({ work, language }: { work: WorkDetail; language: string }) {
  const { t } = useTranslation()
  const format = (iso: string) =>
    new Intl.DateTimeFormat(language, { dateStyle: 'medium' }).format(new Date(iso))
  if (!work.started_at && !work.completed_at) {
    return null
  }
  return (
    <p className="text-sm text-muted-foreground">
      {work.started_at ? t('work.state.started', { date: format(work.started_at) }) : null}
      {work.started_at && work.completed_at ? ' · ' : null}
      {work.completed_at ? t('work.state.completed', { date: format(work.completed_at) }) : null}
    </p>
  )
}
