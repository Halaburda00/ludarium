import { useTranslation } from 'react-i18next'
import { Link, useLocation, useParams } from 'react-router-dom'

import { Metacritic, ScoresCredit, Steam } from '@/components/Scores'
import { ThemePicker } from '@/components/ThemePicker'
import { WorkState } from '@/components/WorkState'
import { Button } from '@/components/ui/button'
import { Notice } from '@/components/ui/field'
import { useWork, type Credit, type EntitlementSummary, type WorkDetail } from '@/lib/queries'

// Who is named first, as the API orders credits: whoever put it out, then
// whoever made it.
const ROLES = ['publisher', 'developer', 'porting', 'support'] as const

/** Where the grid was, so going back returns to the same search rather than the top. */
export type FromLibrary = { search: string } | null

/**
 * One work: everything known about it, and every copy of it the user owns.
 *
 * Owned on several platforms is the premise of the project, so the copies are
 * a table of their own rather than a line under the title.
 */
export default function WorkPage() {
  const { t } = useTranslation()
  const { workId } = useParams()
  const from = useLocation().state as FromLibrary
  // Anything but a positive whole number is no work at all. Not asked about:
  // the answer is known, and the API would call it a malformed request rather
  // than a missing work.
  const id = /^[1-9]\d{0,15}$/.test(workId ?? '') ? Number(workId) : null
  const work = useWork(id ?? 0, id !== null)
  const back = (
    <Link
      to={`/library${from?.search ?? ''}`}
      className="text-sm text-primary underline-offset-4 hover:underline"
    >
      {t('work.back')}
    </Link>
  )

  let body: React.ReactNode
  if (id === null || work.error?.status === 404) {
    body = <p className="text-sm text-muted-foreground">{t('work.notFound')}</p>
  } else if (work.isPending) {
    body = <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
  } else if (work.isError) {
    body = (
      <div className="grid justify-items-start gap-3">
        <Notice>{work.error.detail || t('error.offline')}</Notice>
        <Button variant="outline" onClick={() => void work.refetch()}>
          {t('common.retry')}
        </Button>
      </div>
    )
  } else {
    body = <Details work={work.data} />
  }

  return (
    <main className="mx-auto grid max-w-4xl gap-6 px-6 py-10">
      <header className="flex items-center justify-between gap-4">
        <nav>{back}</nav>
        <ThemePicker />
      </header>
      {body}
    </main>
  )
}

function Details({ work }: { work: WorkDetail }) {
  const { t, i18n } = useTranslation()
  const cover = work.cover
  return (
    <article className="grid gap-8" aria-labelledby="work-title">
      <div className="grid gap-6 sm:grid-cols-[minmax(0,264px)_1fr]">
        <div
          className="w-full max-w-66 overflow-hidden rounded-md bg-muted ring-1 ring-foreground/10"
          style={{ aspectRatio: '264 / 374' }}
        >
          {cover ? (
            <img
              src={cover.url}
              srcSet={cover.url_2x ? `${cover.url} 1x, ${cover.url_2x} 2x` : undefined}
              width={cover.width}
              height={cover.height}
              // The title is the page's heading, right beside it.
              alt=""
              className="h-full w-full object-cover"
            />
          ) : null}
        </div>
        <div className="grid content-start gap-4">
          <h1 id="work-title" className="font-heading text-2xl font-semibold">
            {work.title}
          </h1>
          {work.release_date ? (
            <p className="text-sm text-muted-foreground">
              {t('work.released', { date: releaseDate(work.release_date, i18n.language) })}
            </p>
          ) : work.release_year ? (
            <p className="text-sm text-muted-foreground">
              {t('work.releasedIn', { year: work.release_year })}
            </p>
          ) : null}
          <div className="flex gap-4 text-sm tabular-nums">
            <Metacritic score={work.metacritic} title={work.title} />
            <Steam reviews={work.steam_reviews} title={work.title} />
          </div>
          {work.companies.length > 0 ? <Credits companies={work.companies} /> : null}
          {/* IGDB's text, kept to its paragraphs. */}
          {work.summary ? (
            <p className="text-sm leading-6 whitespace-pre-line">{work.summary}</p>
          ) : null}
        </div>
      </div>
      {/* Keyed on the work, so the notes draft starts again on another game. */}
      <WorkState key={work.id} work={work} />
      <Copies copies={work.entitlements} total={work.playtime_minutes} />
      {/* RAWG's terms: credit and an active link wherever its data is shown. */}
      {work.metacritic ? <ScoresCredit score={work.metacritic} /> : null}
    </article>
  )
}

/** Each role with the companies that played it, publishers first. */
function Credits({ companies }: { companies: Credit[] }) {
  const { t } = useTranslation()
  const roles = ROLES.map((role) => ({
    role,
    names: companies.filter((company) => company.roles.includes(role)).map((c) => c.name),
  })).filter((entry) => entry.names.length > 0)
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {roles.map(({ role, names }) => (
        <div key={role} className="contents">
          <dt className="text-muted-foreground">{t(`work.role.${role}`, { count: names.length })}</dt>
          <dd>{names.join(', ')}</dd>
        </div>
      ))}
    </dl>
  )
}

/** Every copy, with its own playtime and the sum of them (rule 5's exception). */
function Copies({ copies, total }: { copies: EntitlementSummary[]; total: number }) {
  const { t } = useTranslation()
  return (
    <section aria-labelledby="work-copies" className="grid gap-3">
      <h2 id="work-copies" className="font-heading text-lg font-semibold">
        {t('work.copies', { count: copies.length })}
      </h2>
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-border text-xs text-muted-foreground uppercase">
            <th scope="col" className="py-2 pr-4 font-medium">
              {t('work.platform')}
            </th>
            <th scope="col" className="py-2 pr-4 font-medium">
              {t('work.storeTitle')}
            </th>
            <th scope="col" className="py-2 text-right font-medium">
              {t('work.played')}
            </th>
          </tr>
        </thead>
        <tbody>
          {copies.map((copy) => (
            <tr key={copy.id} className="border-b border-border/50">
              <th scope="row" className="py-2 pr-4 font-normal">
                {copy.store_url ? (
                  <a
                    href={copy.store_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={t('library.storeLink', {
                      title: copy.provider_title,
                      provider: copy.provider_name,
                    })}
                    className="text-primary underline-offset-4 hover:underline"
                  >
                    {copy.provider_name}
                  </a>
                ) : (
                  copy.provider_name
                )}
              </th>
              {/* The platform's own name for it, which is not the work's title
                  (rule 5): what the user will find in that store. */}
              <td className="py-2 pr-4">{copy.provider_title}</td>
              <td className="py-2 text-right tabular-nums">
                <Playtime minutes={copy.playtime_minutes} />
              </td>
            </tr>
          ))}
        </tbody>
        {copies.length > 1 ? (
          <tfoot>
            <tr>
              <th scope="row" colSpan={2} className="py-2 pr-4 text-left font-medium">
                {t('work.totalPlayed')}
              </th>
              <td className="py-2 text-right font-medium tabular-nums">
                <Playtime minutes={total} />
              </td>
            </tr>
          </tfoot>
        ) : null}
      </table>
    </section>
  )
}

function Playtime({ minutes }: { minutes: number | null }) {
  const { t } = useTranslation()
  if (!minutes) {
    return <span className="text-muted-foreground">{t('work.notPlayed')}</span>
  }
  return (
    <>{t('work.playtime', { hours: Math.floor(minutes / 60), minutes: minutes % 60 })}</>
  )
}

/**
 * The day, in the reader's language, without the reader's time zone moving it:
 * a date is a date, and at midnight UTC it is the day before in America.
 */
function releaseDate(iso: string, language: string): string {
  return new Intl.DateTimeFormat(language, { dateStyle: 'long', timeZone: 'UTC' }).format(
    new Date(`${iso}T00:00:00Z`),
  )
}
