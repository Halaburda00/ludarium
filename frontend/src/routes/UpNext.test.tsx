import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Router } from '@/App'
import { missingKeys } from '@/i18n'
import { QUEUE_QUERY, type Account, type WorkSummary, type WorksPage } from '@/lib/queries'
import { renderApp, stubFetch } from '@/test/render'

const ACCOUNTS: Account[] = [
  {
    id: 1,
    provider: 'steam',
    label: 'Main',
    external_account_id: '76561197960287930',
    credentials: '••••',
    is_active: true,
    last_success_at: null,
    status: 'success',
    last_error: null,
    created_at: '2026-08-21T10:00:00Z',
    error_kind: null,
    provider_name: 'Steam',
    is_derived: false,
  },
]

const QUEUE = `GET /api/works?${QUEUE_QUERY}`

function queued(id: number, title: string, position: number): WorkSummary {
  return {
    id,
    title,
    sort_title: title,
    is_matched: true,
    item_kind: 'game',
    release_year: 2018,
    play_status: 'queued',
    queue_position: position,
    is_favourite: false,
    is_hidden: false,
    playtime_minutes: id === 2 ? 90 : 0,
    last_played_at: null,
    metacritic: null,
    steam_reviews: null,
    cover: null,
    addon_count: 0,
    entitlements: [
      {
        id: id * 10,
        provider: 'steam',
        provider_name: 'Steam',
        provider_item_id: String(id),
        provider_title: title,
        store_label: null,
        playtime_minutes: null,
        store_url: null,
        kept: false,
      },
    ],
  }
}

// Position 2 belongs to a work whose every copy was removed: it keeps its
// place, and the listing does not show it.
const WORKS = [queued(1, 'Hades', 1), queued(2, 'Celeste', 3), queued(3, 'Minit', 4)]
const PAGE: WorksPage = { works: WORKS, next_cursor: null }

function titles(): string[] {
  return within(screen.getByRole('list'))
    .getAllByRole('listitem')
    .map((item) => within(item).getByRole('link').textContent ?? '')
}

describe('the up next view', () => {
  it('numbers what it shows, in the queue order', async () => {
    stubFetch({ 'GET /api/accounts': { body: ACCOUNTS }, [QUEUE]: { body: PAGE } })
    renderApp(<Router />, { route: '/queue' })

    const items = await screen.findAllByRole('listitem')
    expect(titles()).toEqual(['Hades', 'Celeste', 'Minit'])
    // Shown 1, 2, 3, not the stored 1, 3, 4.
    expect(items.map((item) => item.textContent?.match(/^\d+/)?.[0])).toEqual(['1', '2', '3'])
    expect(items[1]).toHaveTextContent('Steam · 1 h 30 min')
    expect(missingKeys).toEqual([])
  })

  it('reads every page before it numbers anything', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: { works: WORKS.slice(0, 2), next_cursor: 'abc' } },
      [`${QUEUE}&cursor=abc`]: { body: { works: WORKS.slice(2), next_cursor: null } },
    })
    renderApp(<Router />, { route: '/queue' })

    await screen.findAllByRole('listitem')
    expect(titles()).toEqual(['Hades', 'Celeste', 'Minit'])
  })

  it('sends the place of the work it swaps with, not the index', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: PAGE },
      'PUT /api/works/1/queue': { body: { ...WORKS[0], queue_position: 3 } },
    })
    renderApp(<Router />, { route: '/queue' })

    await userEvent.click(await screen.findByRole('button', { name: 'Move Hades down' }))

    await vi.waitFor(() =>
      expect(calls.find((call) => call.method === 'PUT')?.body).toEqual({ position: 3 }),
    )
  })

  it('moves to the top by the first place shown', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: PAGE },
      'PUT /api/works/3/queue': { body: { ...WORKS[2], queue_position: 1 } },
    })
    renderApp(<Router />, { route: '/queue' })

    await userEvent.click(await screen.findByRole('button', { name: 'Move Minit to the top' }))

    await vi.waitFor(() =>
      expect(calls.find((call) => call.method === 'PUT')?.body).toEqual({ position: 1 }),
    )
  })

  it('offers no move past either end', async () => {
    stubFetch({ 'GET /api/accounts': { body: ACCOUNTS }, [QUEUE]: { body: PAGE } })
    renderApp(<Router />, { route: '/queue' })

    expect(await screen.findByRole('button', { name: 'Move Hades up' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Hades to the top' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Minit down' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move Celeste up' })).toBeEnabled()
  })

  it('puts the order back and says why when a move is refused', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: PAGE },
      'PUT /api/works/2/queue': { status: 409, body: { detail: 'the work is not in the queue' } },
    })
    renderApp(<Router />, { route: '/queue' })

    await userEvent.click(await screen.findByRole('button', { name: 'Move Celeste up' }))

    expect(await screen.findByText('the work is not in the queue')).toBeInTheDocument()
    expect(titles()).toEqual(['Hades', 'Celeste', 'Minit'])
  })

  it('takes a work out of the queue by its status', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: PAGE },
      'PATCH /api/works/2/state': { body: { ...WORKS[1], play_status: 'not_started' } },
    })
    renderApp(<Router />, { route: '/queue' })

    await userEvent.click(
      await screen.findByRole('button', { name: 'Take Celeste out of the queue' }),
    )

    await vi.waitFor(() =>
      expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({
        play_status: 'not_started',
      }),
    )
  })

  it('says how to queue a game when nothing is queued', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: { works: [], next_cursor: null } },
    })
    renderApp(<Router />, { route: '/queue' })

    expect(await screen.findByText(/^Nothing queued yet/)).toBeInTheDocument()
  })
})

describe('one move at a time', () => {
  it('waits for the queue to come back before the next move is offered', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: PAGE },
      'PUT /api/works/3/queue': { body: { ...WORKS[2], queue_position: 1 } },
    })
    const fetcher = vi.mocked(fetch)
    const reply = fetcher.getMockImplementation()!
    // Every read of the queue after the first is held until the test lets it go.
    let reads = 0
    let release: () => void = () => {}
    fetcher.mockImplementation((input, init) => {
      if (String(input).startsWith('/api/works?') && ++reads > 1) {
        return new Promise((resolve) => {
          release = () => void reply(input, init).then(resolve)
        })
      }
      return reply(input, init)
    })
    renderApp(<Router />, { route: '/queue' })

    await userEvent.click(await screen.findByRole('button', { name: 'Move Minit to the top' }))

    await vi.waitFor(() => expect(reads).toBe(2))
    expect(screen.getByRole('button', { name: 'Move Hades down' })).toBeDisabled()
    release()
    await vi.waitFor(() =>
      expect(screen.getByRole('button', { name: 'Move Hades down' })).toBeEnabled(),
    )
  })
})

describe('a move after taking a work out', () => {
  it('waits for the queue to come back before the next move is offered', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      [QUEUE]: { body: PAGE },
      'PATCH /api/works/1/state': { body: { ...WORKS[0], play_status: 'not_started' } },
    })
    const fetcher = vi.mocked(fetch)
    const reply = fetcher.getMockImplementation()!
    let reads = 0
    let release: () => void = () => {}
    fetcher.mockImplementation((input, init) => {
      if (String(input).startsWith('/api/works?') && ++reads > 1) {
        return new Promise((resolve) => {
          release = () => void reply(input, init).then(resolve)
        })
      }
      return reply(input, init)
    })
    renderApp(<Router />, { route: '/queue' })

    await userEvent.click(await screen.findByRole('button', { name: 'Take Hades out of the queue' }))

    await vi.waitFor(() => expect(reads).toBe(2))
    // Celeste's stored place is 3 until the queue is read back; the server
    // already has it at 1.
    expect(screen.getByRole('button', { name: 'Move Minit up' })).toBeDisabled()
    release()
    await vi.waitFor(() => expect(screen.getByRole('button', { name: 'Move Minit up' })).toBeEnabled())
  })
})
