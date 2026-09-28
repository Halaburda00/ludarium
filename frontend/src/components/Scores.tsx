import { Trans, useTranslation } from 'react-i18next'

import type { Score, SteamReviews } from '@/lib/queries'

/**
 * The score, as a link to the RAWG page it came from.
 *
 * The link is the attribution RAWG requires, which is why the API serves a
 * score only together with it: this component never has one without the other.
 */
export function Metacritic({ score, title }: { score: Score | null; title: string }) {
  const { t } = useTranslation()
  return (
    <span className="flex gap-1">
      <span className="text-muted-foreground">{t('library.columnMetacritic')}</span>
      {score ? (
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
      ) : (
        <NoScore />
      )}
    </span>
  )
}

/** The share of positive reviews, as a link to them on the store page. */
export function Steam({ reviews, title }: { reviews: SteamReviews | null; title: string }) {
  const { t } = useTranslation()
  if (!reviews) {
    return (
      <span className="flex gap-1">
        <span className="text-muted-foreground">{t('library.steamShort')}</span>
        <NoScore />
      </span>
    )
  }
  const name = t('library.steamReviewsLink', {
    rating: t(`steamRating.${reviews.rating}`),
    title,
    percent: reviews.percent,
    reviews: reviews.count,
  })
  return (
    <span className="flex gap-1">
      <span className="text-muted-foreground">{t('library.steamShort')}</span>
      <a
        href={reviews.url}
        target="_blank"
        rel="noreferrer"
        aria-label={name}
        // For the pointer, which has no other way to read the verdict.
        title={name}
        className="text-primary underline-offset-4 hover:underline"
      >
        {reviews.percent}%
      </a>
    </span>
  )
}

function NoScore() {
  const { t } = useTranslation()
  return (
    <span className="text-muted-foreground">
      <span aria-hidden="true">–</span>
      <span className="sr-only">{t('library.noScore')}</span>
    </span>
  )
}

export function ScoresCredit({ score }: { score: Score }) {
  return (
    <p className="text-xs text-muted-foreground">
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
