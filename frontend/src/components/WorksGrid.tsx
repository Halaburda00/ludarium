import { useWindowVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Metacritic, ScoresCredit, Steam } from '@/components/Scores'
import type { EntitlementSummary, WorkSummary } from '@/lib/queries'
import type { FromLibrary } from '@/routes/WorkPage'

// IGDB's `cover_big`, which every cover the API serves is cropped to (ADR-0024).
// The box is this shape before the image arrives, so nothing moves when it does.
const COVER_WIDTH = 264
const COVER_HEIGHT = 374
// Narrow enough for two columns on a phone, wide enough to read a title.
const MIN_CARD = 160
const GAP = 16
// Title on two lines, platforms on one, scores on one. Fixed rather than
// measured, so a row's height follows from its width alone and the virtualiser
// never has to guess and correct.
const TEXT_BLOCK = 104
// Until the container has been measured, and where it cannot be (jsdom).
const FALLBACK_WIDTH = 1024

// Rows rendered beyond the screen in each direction. Enough that Tab moving
// focus to the next card lands on one that exists: the browser scrolls it into
// view, and the virtualiser renders the rows after it.
const OVERSCAN = 3
// How close to the end of what is loaded the next page is asked for. A page is
// a hundred works, over a dozen rows at any width, so asking with this many
// rows still unread has the answer in before a steady scroll reaches it. A
// faster one reaches the loading row, never a blank one: the list ends where
// the loaded works do.
const PREFETCH_ROWS = 8

type Props = {
  works: WorkSummary[]
  hasMore: boolean
  loadingMore: boolean
  loadFailed: boolean
  onLoadMore: () => void
}

/**
 * The library as a grid of covers, rendering a screenful however large it is.
 *
 * A feed in ARIA's sense — a list that grows as it is read — so each card is an
 * article with its position in the set. The DOM order is the reading order,
 * row by row, and Tab walks it as it would any page. Arrow keys, Home and End
 * move between cards, since a card is several links and tabbing past each one
 * to reach the next title is a long way round.
 */
export function WorksGrid({ works, hasMore, loadingMore, loadFailed, onLoadMore }: Props) {
  const { t } = useTranslation()
  const list = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  const [offset, setOffset] = useState(0)
  const [pendingFocus, setPendingFocus] = useState<number | null>(null)

  useLayoutEffect(() => {
    const element = list.current
    if (!element) return
    const measure = () => {
      setWidth(element.clientWidth)
      // The window scrolls, not the list, so the virtualiser needs to know
      // where the list starts on the page. The notices above it come and go.
      setOffset(element.getBoundingClientRect().top + window.scrollY)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    observer.observe(document.body)
    return () => observer.disconnect()
  }, [])

  const available = width || FALLBACK_WIDTH
  const columns = Math.max(1, Math.floor((available + GAP) / (MIN_CARD + GAP)))
  const cardWidth = (available - GAP * (columns - 1)) / columns
  const rowHeight = Math.round((cardWidth * COVER_HEIGHT) / COVER_WIDTH) + TEXT_BLOCK + GAP
  const rows = Math.ceil(works.length / columns)
  // One more while there is more: the row that says so, and that the list
  // cannot be scrolled past.
  const count = rows + (hasMore ? 1 : 0)

  const virtualizer = useWindowVirtualizer({
    count,
    estimateSize: () => rowHeight,
    overscan: OVERSCAN,
    scrollMargin: offset,
  })
  // The size is a function of the width; a new width is a new size for every row.
  useEffect(() => {
    virtualizer.measure()
  }, [virtualizer, rowHeight])

  const items = virtualizer.getVirtualItems()
  const first = items.length > 0 ? items[0].index : -1
  const last = items.length > 0 ? items[items.length - 1].index : -1
  // Not while one is in flight, and not after one failed: that would ask again
  // on every scroll event. A failed page waits for the button.
  const wanted = hasMore && !loadingMore && !loadFailed && last >= rows - PREFETCH_ROWS
  useEffect(() => {
    if (wanted) onLoadMore()
  }, [wanted, onLoadMore])

  // Focus moves once the card is rendered, which it may not be yet: a card
  // three screens down exists only after the scroll has brought its row in,
  // so this runs again whenever the rendered rows change.
  useEffect(() => {
    if (pendingFocus === null) return
    const card = list.current?.querySelector<HTMLElement>(`[data-work-index="${pendingFocus}"]`)
    if (card) {
      card.focus()
      setPendingFocus(null)
    }
  }, [pendingFocus, first, last])

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const from = (event.target as HTMLElement).closest<HTMLElement>('[data-work-index]')
    if (!from) return
    const index = Number(from.dataset.workIndex)
    const target = {
      ArrowRight: index + 1,
      ArrowLeft: index - 1,
      ArrowDown: index + columns,
      ArrowUp: index - columns,
      Home: 0,
      End: works.length - 1,
    }[event.key]
    if (target === undefined) return
    event.preventDefault()
    const next = Math.min(Math.max(target, 0), works.length - 1)
    virtualizer.scrollToIndex(Math.floor(next / columns), { align: 'auto' })
    setPendingFocus(next)
  }

  const credited = works.find((work) => work.metacritic)?.metacritic
  return (
    <>
      <div
        ref={list}
        role="feed"
        aria-label={t('library.gridLabel')}
        aria-busy={loadingMore}
        onKeyDown={onKeyDown}
        className="relative w-full"
        style={{ height: virtualizer.getTotalSize() }}
      >
        {items.map((row) => (
          <div
            key={row.key}
            data-index={row.index}
            className="absolute top-0 left-0 grid w-full"
            style={{
              transform: `translateY(${row.start - virtualizer.options.scrollMargin}px)`,
              height: rowHeight,
              gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
              columnGap: GAP,
            }}
          >
            {row.index < rows ? (
              works.slice(row.index * columns, (row.index + 1) * columns).map((work, column) => {
                const index = row.index * columns + column
                return (
                  <Card
                    key={work.id}
                    work={work}
                    index={index}
                    // Unknown while pages remain: -1 is ARIA's "not known yet".
                    setSize={hasMore ? -1 : works.length}
                  />
                )
              })
            ) : (
              <div className="col-span-full flex items-start">
                {/* Also a button, and not only a trigger on scroll: a failed
                    page is retried by hand, and a keyboard user reaching the
                    end asks for more the way anyone else would. */}
                <Button variant="outline" onClick={onLoadMore} disabled={loadingMore}>
                  {loadingMore
                    ? t('common.loading')
                    : loadFailed
                      ? t('common.retry')
                      : t('library.loadMore')}
                </Button>
              </div>
            )}
          </div>
        ))}
      </div>
      {/* RAWG's terms: credit and an active link wherever its data is shown.
        Here, below the scores, and only while there are scores to credit. */}
      {credited && <ScoresCredit score={credited} />}
    </>
  )
}

function Card({ work, index, setSize }: { work: WorkSummary; index: number; setSize: number }) {
  const heading = `work-${work.id}-title`
  const { search } = useLocation()
  return (
    <article
      data-work-index={index}
      // Reached by the arrow keys, not by Tab: Tab walks the links inside.
      tabIndex={-1}
      aria-posinset={index + 1}
      aria-setsize={setSize}
      aria-labelledby={heading}
      className="flex min-w-0 flex-col gap-2 rounded-md outline-offset-4 focus-visible:outline-2 focus-visible:outline-ring"
      style={{ height: '100%' }}
    >
      <Cover work={work} />
      {/* `content-start`: the height is fixed, and a grid stretches its rows
        into whatever a one-line title leaves free, which parks the platforms
        halfway down the block. */}
      <div className="grid content-start gap-1 text-sm" style={{ height: TEXT_BLOCK - 8 }}>
        <h2 id={heading} className="line-clamp-2 leading-5 font-medium text-foreground">
          <Link
            to={`/library/${work.id}`}
            // The grid's search, so the way back lands where the user was.
            state={{ search } satisfies FromLibrary}
            className="underline-offset-4 hover:underline"
          >
            {work.title}
          </Link>
        </h2>
        <Platforms copies={work.entitlements} />
        <div className="flex gap-3 text-xs tabular-nums">
          <Metacritic score={work.metacritic} title={work.title} />
          <Steam reviews={work.steam_reviews} title={work.title} />
        </div>
      </div>
    </article>
  )
}

function Cover({ work }: { work: WorkSummary }) {
  const cover = work.cover
  return (
    <div
      className="overflow-hidden rounded-md bg-muted"
      style={{ aspectRatio: `${COVER_WIDTH} / ${COVER_HEIGHT}` }}
    >
      {cover ? (
        <img
          src={cover.url}
          srcSet={cover.url_2x ? `${cover.url} 1x, ${cover.url_2x} 2x` : undefined}
          width={cover.width}
          height={cover.height}
          // Empty: the title is the card's heading, right below. Read twice
          // it is noise.
          alt=""
          loading="lazy"
          decoding="async"
          className="h-full w-full object-cover"
        />
      ) : null}
    </div>
  )
}

/** Every copy of one work. A bundle grants several, so this is a list and not a word. */
function Platforms({ copies }: { copies: EntitlementSummary[] }) {
  const { t } = useTranslation()
  return (
    // One line, cut rather than wrapped: the card's height is fixed.
    <ul className="flex gap-x-3 overflow-hidden text-xs whitespace-nowrap">
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
