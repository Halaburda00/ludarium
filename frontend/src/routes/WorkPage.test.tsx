import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Router } from '@/App'
import { missingKeys } from '@/i18n'
import type { Account, SyncOverview, WorkDetail, WorkSummary } from '@/lib/queries'
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
  },
]
const IDLE: SyncOverview = { providers: [], runs: [], enriching: [] }

function summary(id: number, title: string): WorkSummary {
  return {
    id,
    title,
    sort_title: title,
    is_matched: true,
    item_kind: 'game',
    release_year: 2015,
    play_status: 'not_started',
    is_favourite: false,
    is_hidden: false,
    playtime_minutes: 3365,
    last_played_at: null,
    metacritic: {
      value: 92,
      source_name: 'RAWG',
      source_url: 'https://rawg.io/games/the-witcher-3-wild-hunt',
    },
    steam_reviews: {
      rating: 'overwhelmingly_positive',
      percent: 96,
      count: 824691,
      url: 'https://store.steampowered.com/app/292030#app_reviews_hash',
    },
    cover: null,
    entitlements: [
      {
        id: 10,
        provider: 'steam',
        provider_name: 'Steam',
        provider_item_id: '292030',
        provider_title: 'The Witcher 3: Wild Hunt - Complete Edition',
        playtime_minutes: 3247,
        store_url: 'https://store.steampowered.com/app/292030',
      },
      {
        id: 11,
        provider: 'epic',
        provider_name: 'Epic Games',
        provider_item_id: 'abc',
        provider_title: 'The Witcher 3: Wild Hunt',
        playtime_minutes: 118,
        store_url: null,
      },
    ],
  }
}

const WITCHER: WorkDetail = {
  ...summary(7, 'The Witcher 3: Wild Hunt'),
  summary: 'A monster hunter looks for his adopted daughter.',
  release_date: '2015-05-19',
  genres: [
    { slug: 'adventure', name: 'Adventure' },
    { slug: 'role-playing', name: 'Role-playing' },
  ],
  companies: [
    { name: 'CD Projekt', roles: ['publisher'] },
    { name: 'CD Projekt Red', roles: ['developer'] },
    { name: 'Bandai Namco', roles: ['publisher'] },
  ],
  rating: null,
  notes: null,
  started_at: null,
  completed_at: null,
}

describe('the work page', () => {
  it('shows what is known about the work and every copy of it', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: WITCHER },
    })
    renderApp(<Router />, { route: '/library/7' })

    expect(
      await screen.findByRole('heading', { level: 1, name: 'The Witcher 3: Wild Hunt' }),
    ).toBeInTheDocument()
    expect(screen.getByText('Released May 19, 2015')).toBeInTheDocument()
    expect(screen.getByText(/looks for his adopted daughter/)).toBeInTheDocument()
    // IGDB's English names, since no translation exists for either.
    expect(screen.getByText('Adventure, Role-playing')).toBeInTheDocument()
    // Grouped by role, publishers first, each company once.
    const publishers = screen.getByText('Publishers')
    expect(publishers.tagName).toBe('DT')
    expect(publishers.nextElementSibling).toHaveTextContent('CD Projekt, Bandai Namco')
    expect(screen.getByText('Developer').nextElementSibling).toHaveTextContent('CD Projekt Red')
    // The scores, each with its link, and RAWG credited as its terms require.
    expect(screen.getByRole('link', { name: /Metacritic 92/ })).toHaveAttribute(
      'href',
      'https://rawg.io/games/the-witcher-3-wild-hunt',
    )
    expect(screen.getByRole('link', { name: /Overwhelmingly Positive on Steam/ })).toBeInTheDocument()
    expect(screen.getByText(/Metacritic scores from/)).toBeInTheDocument()
    expect(missingKeys).toEqual([])
  })

  it('lists each copy with its own playtime and the sum of them', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: WITCHER },
    })
    renderApp(<Router />, { route: '/library/7' })

    const copies = await screen.findByRole('table')
    const [, steam, epic, total] = within(copies).getAllByRole('row')
    expect(within(steam).getByRole('link', { name: /on Steam/ })).toHaveAttribute(
      'href',
      'https://store.steampowered.com/app/292030',
    )
    // The store's own name for it, which is not the work's title (rule 5).
    expect(steam).toHaveTextContent('The Witcher 3: Wild Hunt - Complete Edition')
    expect(steam).toHaveTextContent('54 h 7 min')
    // A platform with no store link is still named.
    expect(epic).toHaveTextContent('Epic Games')
    expect(epic).toHaveTextContent('1 h 58 min')
    expect(total).toHaveTextContent('56 h 5 min')
  })

  it('says a work outside the library is not in it', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/99': { status: 404, body: { detail: 'no such work in the library' } },
    })
    renderApp(<Router />, { route: '/library/99' })

    expect(await screen.findByText('This game is not in your library.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '← Library' })).toHaveAttribute('href', '/library')
  })

  it('does not ask about an id that is not one', async () => {
    const calls = stubFetch({ 'GET /api/accounts': { body: ACCOUNTS } })
    renderApp(<Router />, { route: '/library/witcher' })

    expect(await screen.findByText('This game is not in your library.')).toBeInTheDocument()
    expect(calls.map((call) => call.path)).toEqual(['/api/accounts'])
  })

  it('offers to try again when the server fails', async () => {
    let attempt = 0
    stubFetch({ 'GET /api/accounts': { body: ACCOUNTS } })
    const fetcher = globalThis.fetch
    // Three failures, because `useWork` retries a 5xx twice before giving up.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) !== '/api/works/7') return fetcher(input, init)
        attempt += 1
        return attempt <= 3
          ? new Response(JSON.stringify({ detail: 'the database is locked' }), { status: 503 })
          : new Response(JSON.stringify(WITCHER), { status: 200 })
      }),
    )
    renderApp(<Router />, { route: '/library/7' })

    expect(await screen.findByRole('alert')).toHaveTextContent('the database is locked')
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent('The Witcher 3')
  })

  it('is behind the session guard like the library', async () => {
    stubFetch({ 'GET /api/accounts': { status: 401, body: { detail: 'not signed in' } } })
    renderApp(<Router />, { route: '/library/7' })

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('is reached from a card, and the way back keeps the search', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/works?q=witch': { body: { works: [summary(7, 'The Witcher 3: Wild Hunt')], next_cursor: null } },
      'GET /api/works/7': { body: WITCHER },
    })
    renderApp(<Router />, { route: '/library?q=witch' })

    await userEvent.click(await screen.findByRole('link', { name: 'The Witcher 3: Wild Hunt' }))
    await screen.findByRole('heading', { level: 1, name: 'The Witcher 3: Wild Hunt' })
    await userEvent.click(screen.getByRole('link', { name: '← Library' }))

    expect(await screen.findByRole('searchbox', { name: 'Search' })).toHaveValue('witch')
  })
})

describe('what the user decides about a work', () => {
  it('saves a status as soon as it is chosen, and sends nothing else', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: WITCHER },
      'PATCH /api/works/7/state': {
        body: { ...WITCHER, play_status: 'playing', started_at: '2026-09-29T12:00:00Z' },
      },
    })
    renderApp(<Router />, { route: '/library/7' })

    await userEvent.selectOptions(await screen.findByLabelText('Status'), 'Playing')

    expect(await screen.findByText(/^Started /)).toBeInTheDocument()
    const patch = calls.find((call) => call.method === 'PATCH')
    // Only the field that changed: anything else sent would overwrite it.
    expect(patch?.body).toEqual({ play_status: 'playing' })
  })

  it('clears a rating with a null rather than leaving it out', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: { ...WITCHER, rating: 8 } },
      'PATCH /api/works/7/state': { body: WITCHER },
    })
    renderApp(<Router />, { route: '/library/7' })

    await userEvent.selectOptions(await screen.findByLabelText('Rating'), 'Not rated')

    await vi.waitFor(() =>
      expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({ rating: null }),
    )
  })

  it('saves notes on the button, not on every key', async () => {
    const calls = stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: WITCHER },
      'PATCH /api/works/7/state': { body: { ...WITCHER, notes: 'Gwent first' } },
    })
    renderApp(<Router />, { route: '/library/7' })

    await userEvent.type(await screen.findByLabelText('Notes'), 'Gwent first')
    expect(calls.some((call) => call.method === 'PATCH')).toBe(false)
    await userEvent.click(screen.getByRole('button', { name: 'Save notes' }))

    await vi.waitFor(() =>
      expect(calls.find((call) => call.method === 'PATCH')?.body).toEqual({
        notes: 'Gwent first',
      }),
    )
  })

  it('takes the saved notes back from the server, as it stored them', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: WITCHER },
      // Blank notes are stored as none.
      'PATCH /api/works/7/state': { body: { ...WITCHER, notes: null } },
    })
    renderApp(<Router />, { route: '/library/7' })

    await userEvent.type(await screen.findByLabelText('Notes'), '   ')
    await userEvent.click(screen.getByRole('button', { name: 'Save notes' }))

    await vi.waitFor(() =>
      expect(screen.getByRole('button', { name: 'Save notes' })).toBeDisabled(),
    )
    expect(screen.getByLabelText('Notes')).toHaveValue('')
  })

  it('says so when a change was not saved', async () => {
    stubFetch({
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works/7': { body: WITCHER },
      'PATCH /api/works/7/state': { status: 422, body: { detail: 'nothing to change' } },
    })
    renderApp(<Router />, { route: '/library/7' })

    await userEvent.click(await screen.findByLabelText('Favourite'))

    expect(await screen.findByRole('alert')).toHaveTextContent('nothing to change')
  })
})
