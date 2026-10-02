import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import '@/i18n'
import { WorksGrid } from '@/components/WorksGrid'
import type { WorkSummary } from '@/lib/queries'

function work(id: number, overrides: Partial<WorkSummary> = {}): WorkSummary {
  return {
    id,
    title: `Game ${id}`,
    sort_title: `Game ${id}`,
    is_matched: true,
    item_kind: 'game',
    release_year: null,
    play_status: 'not_started',
    is_favourite: false,
    is_hidden: false,
    playtime_minutes: 0,
    last_played_at: null,
    metacritic: null,
    steam_reviews: null,
    cover: null,
    addon_count: 0,
    entitlements: [],
    ...overrides,
  }
}

function library(size: number): WorkSummary[] {
  return Array.from({ length: size }, (_, index) => work(index + 1))
}

function grid(works: WorkSummary[], props: Partial<Parameters<typeof WorksGrid>[0]> = {}) {
  const onLoadMore = vi.fn()
  render(
    <MemoryRouter>
      <WorksGrid
        works={works}
        hasMore={false}
        loadingMore={false}
        loadFailed={false}
        onLoadMore={onLoadMore}
        {...props}
      />
    </MemoryRouter>,
  )
  return onLoadMore
}

describe('the grid', () => {
  it('renders a screenful of a large library, not all of it', () => {
    grid(library(2000))

    const cards = screen.getAllByRole('article')
    // jsdom's window is 1024 × 768: five columns, two rows on screen and three
    // more on either side at most.
    expect(cards.length).toBeGreaterThan(0)
    expect(cards.length).toBeLessThanOrEqual(5 * (2 + 3))
    // Still the whole library as far as a screen reader is concerned.
    expect(cards[0]).toHaveAttribute('aria-setsize', '2000')
    expect(cards[0]).toHaveAttribute('aria-posinset', '1')
  })

  it('does not know the size of a library with pages still to come', () => {
    grid(library(3), { hasMore: true })

    expect(screen.getAllByRole('article')[0]).toHaveAttribute('aria-setsize', '-1')
  })

  it('asks for the next page once the end of what is loaded is near', () => {
    const onLoadMore = grid(library(3), { hasMore: true })

    expect(onLoadMore).toHaveBeenCalledTimes(1)
  })

  it('does not ask again while a page is on its way', () => {
    expect(grid(library(3), { hasMore: true, loadingMore: true })).not.toHaveBeenCalled()
  })

  it('leaves a failed page to a button, rather than asking on every render', async () => {
    const onLoadMore = grid(library(3), { hasMore: true, loadFailed: true })

    expect(onLoadMore).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(onLoadMore).toHaveBeenCalledTimes(1)
  })

  it('does not ask for a page that the API says does not exist', () => {
    expect(grid(library(3))).not.toHaveBeenCalled()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('reserves the cover space before the image arrives, and loads it lazily', () => {
    grid([
      work(1, { cover: { url: '/api/images/1', url_2x: '/api/images/2', width: 264, height: 374 } }),
      work(2),
    ])

    const [covered, bare] = screen.getAllByRole('article')
    const image = covered.querySelector('img')
    expect(image).toHaveAttribute('loading', 'lazy')
    expect(image).toHaveAttribute('srcset', '/api/images/1 1x, /api/images/2 2x')
    expect(image).toHaveAttribute('width', '264')
    expect(image).toHaveAttribute('height', '374')
    // Decorative: the title right below is the card's name.
    expect(image).toHaveAttribute('alt', '')
    // The same box with no image in it, so a card without a cover keeps the
    // row's height.
    const box = (card: HTMLElement) => (card.firstElementChild as HTMLElement).style.aspectRatio
    expect(box(bare)).toBe(box(covered))
    expect(box(bare)).toBe('264 / 374')
  })
})

describe('the keyboard', () => {
  it('moves between cards with the arrow keys, Home and End', async () => {
    // jsdom does not scroll; the virtualiser asks it to.
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    grid(library(12))
    const card = (name: string) => screen.getByRole('article', { name })

    card('Game 1').focus()
    await userEvent.keyboard('{ArrowRight}')
    expect(card('Game 2')).toHaveFocus()
    // Five columns: down is five cards on.
    await userEvent.keyboard('{ArrowDown}')
    expect(card('Game 7')).toHaveFocus()
    await userEvent.keyboard('{End}')
    expect(card('Game 12')).toHaveFocus()
    // Past the end is the end.
    await userEvent.keyboard('{ArrowRight}')
    expect(card('Game 12')).toHaveFocus()
    await userEvent.keyboard('{Home}')
    expect(card('Game 1')).toHaveFocus()
  })

  it('moves from a link inside a card too', async () => {
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    grid([
      work(1, {
        entitlements: [
          {
            id: 1,
            provider: 'steam',
            provider_name: 'Steam',
            provider_item_id: '1',
            provider_title: 'Game 1',
            store_label: null,
            playtime_minutes: 0,
            store_url: 'https://store.steampowered.com/app/1',
            kept: false,
          },
        ],
      }),
      work(2),
    ])

    within(screen.getByRole('article', { name: 'Game 1' }))
      .getByRole('link', { name: 'Game 1 on Steam' })
      .focus()
    await userEvent.keyboard('{ArrowRight}')

    expect(screen.getByRole('article', { name: 'Game 2' })).toHaveFocus()
  })
})
