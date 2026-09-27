import { Trans, useTranslation } from 'react-i18next'

import type { EntitlementSummary, Score, WorkSummary } from '@/lib/queries'

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
  const credited = works.find((work) => work.metacritic)?.metacritic
  return (
    <>
      <table className="w-full border-collapse text-left text-sm">
        {/* The heading above says "Library"; this says what the columns are, to
          the readers who arrive at a table without having read the page. */}
        <caption className="sr-only">{t('library.tableCaption')}</caption>
        <thead>
          <tr className="border-b border-border text-xs text-muted-foreground uppercase">
            <th scope="col" className="py-2 pr-4 font-medium">
              {t('library.columnTitle')}
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              {t('library.columnPlatform')}
            </th>
            <th scope="col" className="py-2 text-right font-medium">
              {t('library.columnMetacritic')}
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
              <td className="py-2 pr-4">
                <Platforms copies={work.entitlements} />
              </td>
              <td className="py-2 text-right tabular-nums">
                <Metacritic score={work.metacritic} title={work.title} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {/* RAWG's terms: credit and an active link wherever its data is shown.
        Here, beside the scores, and only while there are scores to credit. */}
      {credited && <ScoresCredit score={credited} />}
    </>
  )
}

/**
 * The score, as a link to the RAWG page it came from.
 *
 * The link is the attribution RAWG requires, which is why the API serves a
 * score only together with it: this component never has one without the other.
 */
function Metacritic({ score, title }: { score: Score | null; title: string }) {
  const { t } = useTranslation()
  if (!score) {
    return (
      <span className="text-muted-foreground">
        <span aria-hidden="true">–</span>
        <span className="sr-only">{t('library.noScore')}</span>
      </span>
    )
  }
  return (
    <a
      href={score.source_url}
      target="_blank"
      rel="noreferrer"
      aria-label={t('library.scoreLink', {
        score: score.value,
        title,
        source: score.source_name,
      })}
      className="text-primary underline-offset-4 hover:underline"
    >
      {score.value}
    </a>
  )
}

function ScoresCredit({ score }: { score: Score }) {
  return (
    <p className="mt-3 text-xs text-muted-foreground">
      <Trans
        i18nKey="library.scoresCredit"
        values={{ source: score.source_name }}
        // Not `link`: the parser Trans uses reads `<link>` as the void HTML
        // element and closes it at once, leaving the credit with an empty link.
        components={{
          credit: (
            // The source's home, from the page a score links to, so the name
            // and the address come from the same place.
            <a
              href={new URL(score.source_url).origin}
              target="_blank"
              rel="noreferrer"
              className="underline underline-offset-4"
            />
          ),
        }}
      />
    </p>
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
