import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { ThemePicker } from '@/components/ThemePicker'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import { useDemo } from '@/lib/demo'
import {
  useApplyImport,
  usePreviewImport,
  type ImportPreview,
  type ImportResult,
} from '@/lib/queries'

// The columns a file may have, in the order the server documents them.
const COLUMNS = [
  'title',
  'platform',
  'id',
  'ownership_type',
  'item_kind',
  'playtime_minutes',
  'acquired_at',
  'release_year',
] as const

/**
 * A library kept in a spreadsheet or another tool's export, read by the server
 * and shown before anything is written (#130).
 *
 * The file is sent twice, once to preview and once to import, so nothing the
 * user did not confirm is ever stored.
 */
export default function Import() {
  const { t } = useTranslation()
  const demo = useDemo()
  const [file, setFile] = useState<File | null>(null)
  const [sweep, setSweep] = useState(false)
  const preview = usePreviewImport()
  const apply = useApplyImport()

  function choose(chosen: File | null) {
    setFile(chosen)
    setSweep(false)
    apply.reset()
    if (chosen) {
      preview.mutate(chosen)
    } else {
      preview.reset()
    }
  }

  return (
    <main className="mx-auto grid max-w-4xl gap-6 px-6 py-10">
      <header className="flex items-center justify-between gap-4">
        <nav className="flex gap-4">
          <Link to="/library" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('work.back')}
          </Link>
          <Link to="/accounts" className="text-sm text-primary underline-offset-4 hover:underline">
            {t('accounts.link')}
          </Link>
        </nav>
        <ThemePicker />
      </header>
      <div className="grid gap-2">
        <h1 className="font-heading text-2xl font-semibold">{t('import.title')}</h1>
        <p className="text-sm text-muted-foreground">{t('import.intro')}</p>
      </div>
      <Format />
      {demo ? (
        <p className="text-sm text-muted-foreground">{t('import.demo')}</p>
      ) : (
        <div className="grid gap-1.5">
          <label htmlFor="import-file" className="text-sm font-medium">
            {t('import.file')}
          </label>
          <input
            id="import-file"
            type="file"
            accept=".csv,.tsv,.txt,.json,text/csv,application/json"
            onChange={(event) => choose(event.target.files?.[0] ?? null)}
            className="text-sm file:mr-3 file:rounded-md file:border file:border-input file:bg-background file:px-3 file:py-1.5 file:text-sm"
          />
        </div>
      )}
      {preview.isPending ? (
        <p className="text-sm text-muted-foreground">{t('import.reading')}</p>
      ) : null}
      {preview.isError ? <Notice>{preview.error.detail}</Notice> : null}
      {preview.data && file && !apply.data ? (
        <Preview
          preview={preview.data}
          sweep={sweep}
          onSweep={setSweep}
          busy={apply.isPending}
          onImport={() => apply.mutate({ file, sweep })}
        />
      ) : null}
      {apply.isError ? <Notice>{apply.error.detail}</Notice> : null}
      {apply.data ? <Outcome result={apply.data} /> : null}
    </main>
  )
}

function Format() {
  const { t } = useTranslation()
  return (
    <details className="rounded-lg border border-border p-4 text-sm">
      <summary className="cursor-pointer font-medium">{t('import.format.title')}</summary>
      <div className="mt-3 grid gap-3">
        <p className="text-muted-foreground">{t('import.format.intro')}</p>
        <dl className="grid gap-2 sm:grid-cols-[max-content_1fr] sm:gap-x-4">
          {COLUMNS.map((column) => (
            <div key={column} className="contents">
              <dt className="font-mono text-xs">{column}</dt>
              <dd className="text-muted-foreground">{t(`import.format.columns.${column}`)}</dd>
            </div>
          ))}
        </dl>
        <p className="text-muted-foreground">{t('import.format.encoding')}</p>
      </div>
    </details>
  )
}

function Preview({
  preview,
  sweep,
  onSweep,
  busy,
  onImport,
}: {
  preview: ImportPreview
  sweep: boolean
  onSweep: (sweep: boolean) => void
  busy: boolean
  onImport: () => void
}) {
  const { t } = useTranslation()
  const importable = preview.groups.filter(
    (group) => group.status === 'new' || group.status === 'existing',
  )
  const games = importable.reduce((total, group) => total + group.items, 0)
  // Connected platforms count too: a sweep retires what an earlier import put there.
  const removable = preview.groups.reduce((total, group) => total + group.would_remove, 0)
  const more = preview.problem_count - preview.problems.length

  return (
    <section className="grid gap-4" aria-labelledby="import-preview">
      <h2 id="import-preview" className="font-heading text-lg font-semibold">
        {t('import.preview')}
      </h2>
      <p className="text-sm">
        {t('import.read', {
          count: preview.rows_read,
          format: preview.format.toUpperCase(),
          encoding: preview.encoding ?? '',
        })}
        {preview.delimiter
          ? ' ' +
            t('import.delimiter', {
              delimiter: preview.delimiter === '\t' ? t('import.tab') : preview.delimiter,
            })
          : null}
      </p>
      {preview.ignored_columns.length ? (
        <Notice tone="warn">
          {t('import.ignored', { columns: preview.ignored_columns.join(', ') })}
        </Notice>
      ) : null}
      {preview.problem_count ? (
        <div className="grid gap-1">
          <Notice tone="warn">{t('import.problems', { count: preview.problem_count })}</Notice>
          <ul className="grid gap-0.5 text-sm">
            {preview.problems.map((problem) => (
              <li key={problem.row}>
                {t('import.problem', { row: problem.row, message: problem.message })}
              </li>
            ))}
          </ul>
          {more > 0 ? (
            <p className="text-xs text-muted-foreground">{t('import.moreProblems', { count: more })}</p>
          ) : null}
        </div>
      ) : null}
      {preview.sample.length ? (
        <table className="w-full text-left text-sm">
          <caption className="mb-1 text-left text-xs text-muted-foreground">
            {t('import.sample')}
          </caption>
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th className="py-1 pr-3 font-normal">{t('import.row')}</th>
              <th className="py-1 pr-3 font-normal">{t('import.format.columns.titleName')}</th>
              <th className="py-1 font-normal">{t('import.format.columns.platformName')}</th>
            </tr>
          </thead>
          <tbody>
            {preview.sample.map((row) => (
              <tr key={row.row} className="border-t border-border">
                <td className="py-1 pr-3 tabular-nums">{row.row}</td>
                <td className="py-1 pr-3">{row.title}</td>
                <td className="py-1">{row.platform}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <ul className="grid gap-2">
        {preview.groups.map((group) => (
          <li
            key={`${group.provider}:${group.label}`}
            className="flex flex-wrap items-baseline justify-between gap-2 rounded-lg border border-border p-3 text-sm"
          >
            <span className="font-medium">
              {t('import.account', { provider: group.provider_name, label: group.label })}
            </span>
            <span className="text-muted-foreground">
              {t(`import.status.${group.status}`, {
                count: group.items,
                provider: group.provider_name,
              })}
            </span>
          </li>
        ))}
      </ul>
      <div className="grid gap-1">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={sweep}
            disabled={!preview.can_sweep || removable === 0}
            onChange={(event) => onSweep(event.target.checked)}
          />
          {t('import.sweep')}
        </label>
        <p className="text-xs text-muted-foreground">
          {preview.can_sweep
            ? t('import.sweepHint', { count: removable })
            : t('import.sweepUnavailable')}
        </p>
      </div>
      <div>
        <Button disabled={busy || games === 0} onClick={onImport}>
          {t('import.apply', { count: games })}
        </Button>
      </div>
    </section>
  )
}

function Outcome({ result }: { result: ImportResult }) {
  const { t } = useTranslation()
  return (
    <section className="grid gap-3" aria-labelledby="import-done">
      <h2 id="import-done" className="font-heading text-lg font-semibold">
        {t('import.done')}
      </h2>
      <ul className="grid gap-2 text-sm">
        {result.outcomes.map((outcome) => (
          <li key={`${outcome.provider}:${outcome.label}`}>
            <span className="font-medium">{outcome.label}</span>
            {': '}
            {outcome.run
              ? outcome.run.status === 'success'
                ? t('import.counts', {
                    added: outcome.run.items_added,
                    updated: outcome.run.items_updated,
                    removed: outcome.run.items_removed,
                  })
                : t('import.failed', { reason: outcome.run.error_text ?? outcome.run.status })
              : (outcome.detail ?? t(`import.skipped.${outcome.status}`))}
          </li>
        ))}
      </ul>
      <Link to="/library" className="text-sm text-primary underline-offset-4 hover:underline">
        {t('import.toLibrary')}
      </Link>
    </section>
  )
}
