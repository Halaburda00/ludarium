import { useTranslation } from 'react-i18next'

import type { EntitlementSummary, WorkSummary } from '@/lib/queries'

/**
 * The library as a table, because the library is tabular.
 *
 * No virtualisation, no covers, no filters — the grid arrives in M2. A `<table>`
 * is what survives being read row by row by a screen reader, searched with the
 * browser's own find, and printed; a list of divs is none of those things for
 * the sake of looking better sooner.
 */
export function WorksTable({ works }: { works: WorkSummary[] }) {
  const { t } = useTranslation()
  return (
    <table className="w-full border-collapse text-left text-sm">
      {/* The heading above says "Library"; this says what the columns are, to
          the readers who arrive at a table without having read the page. */}
      <caption className="sr-only">{t('library.tableCaption')}</caption>
      <thead>
        <tr className="border-b border-border text-xs text-muted-foreground uppercase">
          <th scope="col" className="py-2 pr-4 font-medium">
            {t('library.columnTitle')}
          </th>
          <th scope="col" className="py-2 font-medium">
            {t('library.columnPlatform')}
          </th>
        </tr>
      </thead>
      <tbody>
        {works.map((work) => (
          <tr key={work.id} className="border-b border-border/50">
            {/* A row header, not a cell: the title is what identifies the row,
                and it is what a screen reader should announce alongside the
                platform rather than leaving "Steam" to stand on its own. */}
            <th scope="row" className="py-2 pr-4 font-normal text-foreground">
              {work.title}
            </th>
            <td className="py-2">
              <Platforms copies={work.entitlements} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Every copy of one work. A bundle grants several, so this is a list and not a word. */
function Platforms({ copies }: { copies: EntitlementSummary[] }) {
  const { t } = useTranslation()
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1">
      {copies.map((copy) => (
        <li key={copy.id}>
          {copy.store_url ? (
            <a
              href={copy.store_url}
              // We never launch a game, so the store page is the answer to
              // "where do I find this" — and it belongs in its own tab, because
              // the library is the thing the user was in the middle of.
              target="_blank"
              rel="noreferrer"
              // Without this every link in the column is named "Steam", and a
              // screen reader's list of links says "Steam" forty times. The
              // platform's own name for the copy is what distinguishes them.
              aria-label={t('library.storeLink', {
                title: copy.provider_title,
                provider: copy.provider_name,
              })}
              className="text-primary underline-offset-4 hover:underline"
            >
              {copy.provider_name}
            </a>
          ) : (
            // No template, or nothing to put in it. Still a platform worth
            // naming: the user owns it there whether or not we can link it.
            <span className="text-muted-foreground">{copy.provider_name}</span>
          )}
        </li>
      ))}
    </ul>
  )
}
