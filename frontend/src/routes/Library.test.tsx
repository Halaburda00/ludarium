import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  Account,
  EntitlementSummary,
  SteamReviews,
  SyncOverview,
  SyncResult,
  SyncRun,
  SavedView,
  WorkSummary,
  WorksPage,
} from '@/lib/queries'
import i18n from '@/i18n'
import { ENRICHMENT_POLL_MS } from '@/lib/queries'
import Library, { SEARCH_DEBOUNCE_MS } from '@/routes/Library'
import { renderApp, stubFetch } from '@/test/render'
import { useNavigate } from 'react-router-dom'

// Typed, so that a field the backend renames is a compile error here rather
// than a fixture and a type quietly drifting together (#35).
const EMPTY: WorksPage = { works: [], next_cursor: null }

function copy(overrides: Partial<EntitlementSummary> = {}): EntitlementSummary {
  return {
    id: 1,
    provider: 'steam',
    provider_name: 'Steam',
    provider_item_id: '570',
    provider_title: 'Dota 2',
    playtime_minutes: 120,
    store_url: 'https://store.steampowered.com/app/570',
    ...overrides,
  }
}

function work(
  id: number,
  title: string,
  entitlements: EntitlementSummary[] = [copy({ id, provider_title: title })],
): WorkSummary {
  return {
    id,
    title,
    sort_title: title.toLowerCase(),
    is_matched: false,
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
    entitlements,
  }
}

const THREE: WorksPage = {
  works: [work(1, 'Dota 2'), work(2, 'Portal 2'), work(3, 'The Witcher 3: Wild Hunt')],
  next_cursor: null,
}

function run(overrides: Partial<SyncRun> = {}): SyncRun {
  return {
    id: 1,
    provider: 'steam',
    account_id: 1,
    trigger: 'manual',
    status: 'success',
    started_at: '2026-08-21T10:00:00Z',
    finished_at: '2026-08-21T10:00:04Z',
    items_seen: 3,
    items_added: 3,
    items_updated: 0,
    items_removed: 0,
    items_skipped: 0,
    error_text: null,
    error_kind: null,
    ...overrides,
  }
}

/** A sync's answer: its runs, and the steps it queued after them. */
function synced(...runs: SyncRun[]): SyncResult {
  return { runs, enriching: [] }
}

/** `GET /api/sync/runs`: which steps are still running, and the runs so far. */
function overview(enriching: string[] = [], runs: SyncRun[] = []): SyncOverview {
  const provider = (key: string, display_name: string) => ({
    key,
    display_name,
    enabled: true,
    status: 'success' as const,
    last_success_at: null,
    last_error: null,
  })
  return {
    providers: [provider('steam', 'Steam'), provider('igdb', 'IGDB'), provider('rawg', 'RAWG')],
    runs,
    enriching,
  }
}

const IDLE = overview()

function account(provider: string, id = 1): Account {
  return {
    id,
    provider,
    external_account_id: `${provider}-account`,
    label: 'Main',
    is_active: true,
    created_at: '2026-08-21T09:00:00Z',
    last_success_at: null,
    status: 'success',
    last_error: null,
    credentials: '••••••••',
  }
}

const ACCOUNTS = [account('steam')]

describe('library', () => {
  it('asks for a sync and reports what the run saw', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: synced(run()) },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Synced 3 games.')
    expect(calls.some((call) => call.method === 'POST' && call.path === '/api/sync/steam')).toBe(
      true,
    )
  })

  it('reports a run that failed rather than pretending it worked', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: EMPTY },
      'POST /api/sync/steam': {
        body: synced(run({ status: 'failed', items_seen: 0, error_text: 'steam did not answer' })),
      },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    // 200 with a failed run is the shape rule 4 gives this: the request
    // succeeded, the sync did not, and the screen has to say the second thing.
    expect(await screen.findByRole('alert')).toHaveTextContent('steam did not answer')
  })

  it('treats a 409 as news rather than as an error', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: EMPTY },
      'POST /api/sync/steam': { status: 409, body: { detail: 'account 1 is already syncing' } },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    // Something is already doing the thing that was asked for, which is not a
    // failure to explain in the backend's words.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A sync is already running for this account.',
    )
  })

  it('says the library is empty rather than showing nothing at all', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: EMPTY },
    })
    renderApp(<Library />)

    expect(await screen.findByText(/Nothing here yet/)).toBeInTheDocument()
  })

  it('lists what came back, with the count pluralised', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    expect(await screen.findByText('3 games')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'The Witcher 3: Wild Hunt' })).toBeInTheDocument()
  })

  it('signs out through the endpoint rather than by forgetting locally', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'POST /api/auth/logout': { status: 204 },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    // The cookie is httpOnly, so there is nothing local to forget: the session
    // ends because the server deleted the row. Where the user goes next is
    // `App.test.tsx`'s subject.
    await waitFor(() => expect(calls.some((call) => call.path === '/api/auth/logout')).toBe(true))
  })
})

describe('a partial run', () => {
  it('is not reported as a success', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: synced(run({ status: 'partial', items_seen: 2 })) },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    // "Synced 2 games" over a partial run tells the user everything arrived.
    // Some of their library is missing and only the next run may fix it.
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('did not hand over all of it')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says how many entries could not be read', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': {
        body: synced(run({ status: 'partial', items_seen: 196, items_skipped: 1 })),
      },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Synced 196 games, but the platform did not hand over all of it. ' +
        '1 entry it sent could not be read, so nothing was marked as removed.',
    )
  })
})

describe('the grid', () => {
  it('gives every game a card with its title and its platform, in order', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    // Found by role rather than by text: a `<div>` full of titles would satisfy
    // `getByText` just as well, and a screen reader could not tell one card
    // from the next.
    const feed = await screen.findByRole('feed', { name: 'Your games' })
    const cards = within(feed).getAllByRole('article')
    expect(cards.map((card) => card.getAttribute('aria-posinset'))).toEqual(['1', '2', '3'])
    expect(cards[0]).toHaveAccessibleName('Dota 2')
    expect(cards[0]).toHaveAttribute('aria-setsize', '3')
    expect(within(cards[0]).getByRole('heading', { name: 'Dota 2' })).toBeInTheDocument()
    expect(within(cards[0]).getByRole('link', { name: 'Dota 2 on Steam' })).toHaveTextContent(
      'Steam',
    )
  })

  it('links the platform to the store page the API built', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    const link = await screen.findByRole('link', { name: 'Dota 2 on Steam' })
    expect(link).toHaveAttribute('href', 'https://store.steampowered.com/app/570')
    // The store page opens beside the library, not on top of it, and a new tab
    // must not inherit a handle on this one.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noreferrer'))
  })

  it('names a platform it cannot link rather than dropping it', async () => {
    const unlinkable: WorksPage = {
      works: [work(1, 'Disc Copy', [copy({ store_url: null, provider_name: 'GOG' })])],
      next_cursor: null,
    }
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: unlinkable },
    })
    renderApp(<Library />)

    // A missing store template is our gap, not the user's: they still own it
    // there, and an empty platform cell reads as if they do not.
    expect(await screen.findByText('GOG')).toBeInTheDocument()
    expect(
      within(screen.getByRole('feed')).queryByRole('link', { name: /on GOG/ }),
    ).not.toBeInTheDocument()
  })

  it('shows every copy of a work owned twice', async () => {
    const both: WorksPage = {
      works: [
        work(1, 'Portal 2', [
          copy({ id: 10, provider_name: 'Steam' }),
          copy({ id: 11, provider: 'gog', provider_name: 'GOG', store_url: null }),
        ]),
      ],
      next_cursor: null,
    }
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: both },
    })
    renderApp(<Library />)

    // One work, two entitlements — a bundle or a rebuy. Collapsing them to the
    // first would quietly answer "where do I own this" with half the truth.
    const card = await screen.findByRole('article', { name: 'Portal 2' })
    expect(within(card).getByRole('link', { name: /on Steam/ })).toHaveTextContent('Steam')
    expect(within(card).getByText('GOG')).toBeInTheDocument()
  })
})

describe('Steam reviews', () => {
  const PORTAL: WorksPage = {
    works: [
      {
        ...work(1, 'Portal 2'),
        steam_reviews: {
          rating: 'overwhelmingly_positive',
          percent: 98,
          count: 390695,
          url: 'https://store.steampowered.com/app/620#app_reviews_hash',
        },
      },
      work(2, 'Dota 2'),
    ],
    next_cursor: null,
  }

  it('links the share of positive reviews to them on the store page', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: PORTAL },
    })
    renderApp(<Library />)

    const score = await screen.findByRole('link', {
      name: 'Overwhelmingly Positive on Steam for Portal 2: 98% of 390,695 reviews positive',
    })
    expect(score).toHaveTextContent('98%')
    expect(score).toHaveAttribute(
      'href',
      'https://store.steampowered.com/app/620#app_reviews_hash',
    )
    expect(score).toHaveAttribute('rel', expect.stringContaining('noreferrer'))
  })

  it('shows how few reviews a score without a verdict rests on', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': {
        body: {
          works: [
            {
              ...work(1, 'Tiny Game'),
              steam_reviews: {
                rating: null,
                percent: 100,
                count: 3,
                url: 'https://store.steampowered.com/app/9#app_reviews_hash',
              },
            },
          ],
          next_cursor: null,
        },
      },
    })
    renderApp(<Library />)

    const score = await screen.findByRole('link', {
      name: 'No verdict on Steam for Tiny Game, too few reviews: 100% of 3 reviews positive',
    })
    expect(score).toHaveTextContent('100% · 3 reviews')
  })

  it('names every verdict the store can give', () => {
    // A record rather than a list, so a verdict the API gains fails to compile here.
    const verdicts: Record<NonNullable<SteamReviews['rating']>, null> = {
      overwhelmingly_negative: null,
      very_negative: null,
      negative: null,
      mostly_negative: null,
      mixed: null,
      mostly_positive: null,
      positive: null,
      very_positive: null,
      overwhelmingly_positive: null,
    }
    for (const rating of Object.keys(verdicts)) {
      expect(i18n.exists(`steamRating.${rating}`)).toBe(true)
    }
  })
})

describe('Metacritic', () => {
  const PORTAL: WorksPage = {
    works: [
      {
        ...work(1, 'Portal 2'),
        metacritic: {
          value: 95,
          source_name: 'RAWG',
          source_url: 'https://rawg.io/games/portal-2',
        },
      },
      work(2, 'Dota 2'),
    ],
    next_cursor: null,
  }

  it('links each score to the RAWG page it is credited to', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: PORTAL },
    })
    renderApp(<Library />)

    const score = await screen.findByRole('link', { name: 'Metacritic 95 for Portal 2, from RAWG' })
    expect(score).toHaveTextContent('95')
    expect(score).toHaveAttribute('href', 'https://rawg.io/games/portal-2')
    expect(score).toHaveAttribute('rel', expect.stringContaining('noreferrer'))
    const dota = screen.getByRole('article', { name: 'Dota 2' })
    // Metacritic's cell and Steam's: neither has anything to say about Dota.
    expect(within(dota).getAllByText('No score')).toHaveLength(2)
  })

  it('credits RAWG with a link wherever a score is shown', async () => {
    // RAWG's terms, not a nicety: attribution and an active link.
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: PORTAL },
    })
    renderApp(<Library />)

    const credit = await screen.findByText(/Metacritic scores from/)
    expect(within(credit).getByRole('link', { name: 'RAWG' })).toHaveAttribute(
      'href',
      'https://rawg.io',
    )
  })

  it('credits nobody when there is no score to credit', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    await screen.findByText('Portal 2')
    expect(screen.queryByText(/Metacritic scores from/)).not.toBeInTheDocument()
  })
})

describe('search', () => {
  it('asks the server once typing pauses, and says how many matched', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'GET /api/works?q=witch': {
        body: { works: [work(3, 'The Witcher 3: Wild Hunt')], next_cursor: null },
      },
    })
    renderApp(<Library />)
    await screen.findByText('3 games')

    await userEvent.type(screen.getByRole('searchbox', { name: 'Search' }), 'witch')

    expect(await screen.findByText('1 match')).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(1)
    // The server searches, not the page: it holds the rest of the library.
    // And once, for the word, not once per letter typed.
    expect(calls.filter((call) => call.path.startsWith('/api/works?q='))).toHaveLength(1)
  })

  it('says nothing matched rather than calling the library empty', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works?q=zelda': { body: EMPTY },
    })
    renderApp(<Library />, { route: '/?q=zelda' })

    expect(await screen.findByText('Nothing in the library matches “zelda”.')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing here yet/)).not.toBeInTheDocument()
    // The search is still there to change: it came from the address.
    expect(screen.getByRole('searchbox', { name: 'Search' })).toHaveValue('zelda')
  })

  it('pages a search on its own cursor', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works?q=2': { body: { works: [work(1, 'Dota 2')], next_cursor: 'next' } },
      'GET /api/works?q=2&cursor=next': {
        body: { works: [work(2, 'Portal 2')], next_cursor: null },
      },
    })
    renderApp(<Library />, { route: '/?q=2' })

    expect(await screen.findByText('2 matches')).toBeInTheDocument()
  })

  it('is not offered over a library with nothing in it', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: EMPTY },
    })
    renderApp(<Library />)

    await screen.findByText(/Nothing here yet/)
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  })
})

describe('paging', () => {
  it('follows the cursor the API handed back', async () => {
    const first: WorksPage = { works: [work(1, 'Dota 2')], next_cursor: 'page-2==' }
    const second: WorksPage = { works: [work(2, 'Portal 2')], next_cursor: null }
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: first },
      // Percent-encoded, because the cursor is base64url and its padding is not
      // safe in a query string unescaped.
      'GET /api/works?cursor=page-2%3D%3D': { body: second },
    })
    renderApp(<Library />)

    // Asked for without a click: the end of what is loaded is on screen.
    expect(await screen.findByText('2 games')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'Portal 2' })).toBeInTheDocument()
    // Appended, not replaced: the first page is still on screen.
    expect(screen.getByRole('article', { name: 'Dota 2' })).toBeInTheDocument()
    expect(calls.filter((call) => call.path.startsWith('/api/works'))).toHaveLength(2)
  })

  it('keeps what it has when a later page fails, and offers to try again', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: { works: [work(1, 'Dota 2')], next_cursor: 'page-2' } },
      'GET /api/works?cursor=page-2': { status: 503, body: { detail: 'the database is locked' } },
    })
    renderApp(<Library />)

    // The first page is still the user's to read: a failed second page is not
    // a library that failed to load.
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'Dota 2' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // Three attempts, `useWorks`' own retries, and then it waits for the button.
    const asked = () => calls.filter((call) => call.path.includes('cursor')).length
    expect(asked()).toBe(3)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(asked()).toBe(3)
  })

  it('offers nothing more to load on the last page', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    expect(await screen.findByText('3 games')).toBeInTheDocument()
    // A null cursor is the end of the library. A button here would ask for a
    // page the API has already said does not exist.
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  })
})

describe('a library that would not load', () => {
  it('says so and offers to try again, rather than looking empty', async () => {
    // Three failures, because `useWorks` retries a 5xx twice before giving
    // up: a single one is swallowed and the screen never reaches the state
    // under test.
    let attempt = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === '/api/sync/runs') {
          return new Response(JSON.stringify(IDLE), { status: 200 })
        }
        if (String(input) === '/api/accounts') {
          return new Response(JSON.stringify(ACCOUNTS), { status: 200 })
        }
        if (String(input) === '/api/views') {
          return new Response(JSON.stringify([]), { status: 200 })
        }
        attempt += 1
        return attempt <= 3
          ? new Response(JSON.stringify({ detail: 'the database is locked' }), { status: 503 })
          : new Response(JSON.stringify(THREE), { status: 200 })
      }),
    )
    renderApp(<Library />)

    // "Nothing here yet" over a failed request tells the user their library is
    // gone. It is the one message this screen must not get wrong.
    expect(await screen.findByRole('alert')).toHaveTextContent('the database is locked')
    expect(screen.queryByText(/Nothing here yet/)).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('article', { name: 'Dota 2' })).toBeInTheDocument()
  })
})

describe('a page that came back empty with a cursor after it', () => {
  it('offers the next page rather than declaring the library empty', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      // Exactly what `works.py` answers when a torn read drops every row of a
      // page: no works, and a cursor saying the library continues. It takes the
      // cursor from the last row read rather than the last row kept for this
      // case specifically, so a client that stops here truncates the library.
      'GET /api/works': { body: { works: [], next_cursor: 'page-2' } satisfies WorksPage },
      'GET /api/works?cursor=page-2': {
        body: { works: [work(1, 'Dota 2')], next_cursor: null } satisfies WorksPage,
      },
    })
    renderApp(<Library />)

    // Asked for on its own: an empty page with a cursor is the end of what is
    // loaded, and the end of what is loaded is on screen.
    expect(await screen.findByRole('article', { name: 'Dota 2' })).toBeInTheDocument()
    // "Nothing here yet. Run a sync" over a library that is merely a page
    // further on sends the user to connect an account they already have.
    expect(screen.queryByText(/Nothing here yet/)).not.toBeInTheDocument()
  })

  it('still calls a library with nothing in it empty', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: EMPTY },
    })
    renderApp(<Library />)

    // The fix above must not turn the empty state off altogether: no cursor is
    // the end of the library, and there the message is the right one.
    expect(await screen.findByText(/Nothing here yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  })
})

describe('the sync button', () => {
  it('is freed by the sync finishing, not by the refetch that follows it', async () => {
    const seen: string[] = []
    let syncing = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input)
        seen.push(path)
        if (path === '/api/sync/runs') {
          return new Response(JSON.stringify(IDLE), { status: 200 })
        }
        if (path === '/api/accounts') {
          return new Response(JSON.stringify(ACCOUNTS), { status: 200 })
        }
        if (path === '/api/views') {
          return new Response(JSON.stringify([]), { status: 200 })
        }
        if (path.startsWith('/api/sync')) {
          syncing = true
          return new Response(JSON.stringify(synced(run())), { status: 200 })
        }
        const cursor = new URL(path, 'http://x').searchParams.get('cursor')
        const page = cursor === null ? 1 : Number(cursor)
        // The refetch that follows a sync replays every loaded page, and it
        // never resolves here — so anything still disabled at that point is
        // disabled by the refetch rather than by the sync.
        if (syncing) return new Promise<Response>(() => {})
        return new Response(
          JSON.stringify({
            works: [work(page, `Game ${page}`)],
            next_cursor: page < 2 ? String(page + 1) : null,
          } satisfies WorksPage),
          { status: 200 },
        )
      }),
    )
    renderApp(<Library />)

    await screen.findByText('2 games')
    await userEvent.click(screen.getByRole('button', { name: 'Sync now' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Sync now' })).toBeEnabled())
  })
})

describe('the steps after a sync', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('says which one is running and holds the sync button back', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: overview(['rawg']) },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    expect(
      await screen.findByText(
        'Still updating the library: asking RAWG. The list refreshes when it is done.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Updating…' })).toBeDisabled()
  })

  it('refetches the library once the last one has finished', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const scored: WorksPage = {
      works: [
        {
          ...work(1, 'Portal 2'),
          metacritic: {
            value: 95,
            source_name: 'RAWG',
            source_url: 'https://rawg.io/games/portal-2',
          },
        },
      ],
      next_cursor: null,
    }
    const routes: Record<string, { body: unknown }> = {
      'GET /api/sync/runs': { body: overview(['rawg']) },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: { works: [work(1, 'Portal 2')], next_cursor: null } },
    }
    stubFetch(routes)
    renderApp(<Library />)
    await screen.findByText(/asking RAWG/)

    routes['GET /api/sync/runs'] = { body: IDLE }
    routes['GET /api/works'] = { body: scored }
    await vi.advanceTimersByTimeAsync(ENRICHMENT_POLL_MS)

    expect(await screen.findByRole('link', { name: /Metacritic 95/ })).toBeInTheDocument()
    expect(screen.queryByText(/asking RAWG/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sync now' })).toBeEnabled()
  })

  it('reports a step this sync queued that failed', async () => {
    const failed = run({
      id: 7,
      provider: 'igdb',
      account_id: null,
      status: 'failed',
      started_at: '2026-08-21T10:00:05Z',
      finished_at: '2026-08-21T10:00:06Z',
      error_text: 'igdb answered 503',
    })
    stubFetch({
      'GET /api/sync/runs': { body: overview([], [failed]) },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: { runs: [run()], enriching: ['igdb'] } },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    expect(await screen.findByText('IGDB did not finish: igdb answered 503')).toBeInTheDocument()
  })

  it('does not blame this sync for a step that failed before it', async () => {
    const earlier = run({
      id: 7,
      provider: 'igdb',
      account_id: null,
      status: 'failed',
      started_at: '2026-08-20T09:00:00Z',
      finished_at: '2026-08-20T09:00:01Z',
      error_text: 'igdb answered 503',
    })
    stubFetch({
      'GET /api/sync/runs': { body: overview([], [earlier]) },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: { runs: [run()], enriching: ['igdb'] } },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    await screen.findByText('Synced 3 games.')
    expect(screen.queryByText(/did not finish/)).not.toBeInTheDocument()
  })
})

describe('the moment a sync answers', () => {
  it('holds the button back before the overview has caught up', async () => {
    // Freed in that gap, a second click would start a sync whose answer
    // replaces this one's, and a failure of a step this one queued would go
    // unreported.
    let synced_ = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input)
        if (path === '/api/accounts') {
          return new Response(JSON.stringify(ACCOUNTS), { status: 200 })
        }
        if (path === '/api/views') {
          return new Response(JSON.stringify([]), { status: 200 })
        }
        if (path === '/api/sync/runs') {
          return synced_
            ? new Promise<Response>(() => {})
            : new Response(JSON.stringify(IDLE), { status: 200 })
        }
        if (init?.method === 'POST') {
          synced_ = true
          return new Response(JSON.stringify({ runs: [run()], enriching: ['rawg'] }), {
            status: 200,
          })
        }
        return new Response(JSON.stringify(THREE), { status: 200 })
      }),
    )
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    await screen.findByText('Synced 3 games.')
    expect(screen.getByRole('button', { name: 'Updating…' })).toBeDisabled()
  })
})

describe('more than one platform', () => {
  const BOTH = [account('steam', 1), account('epic', 2)]
  const epicRun = (overrides: Partial<SyncRun> = {}) =>
    run({ id: 2, provider: 'epic', account_id: 2, items_seen: 5, ...overrides })
  const named = {
    ...IDLE,
    providers: [
      ...IDLE.providers,
      { ...IDLE.providers[0], key: 'epic', display_name: 'Epic Games' },
    ],
  }

  it('syncs every platform an account is connected on', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: named },
      'GET /api/accounts': { body: BOTH },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: synced(run()) },
      'POST /api/sync/epic': { body: synced(epicRun()) },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    expect(await screen.findByText('Synced 8 games.')).toBeInTheDocument()
    const posted = calls.filter((call) => call.method === 'POST').map((call) => call.path)
    expect(posted).toEqual(['/api/sync/steam', '/api/sync/epic'])
  })

  it('offers to sign in again when the sign-in ended, and not when Epic is down', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: named },
      'GET /api/accounts': { body: BOTH },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': {
        body: synced(
          run({ status: 'failed', error_text: 'steam answered 503', error_kind: 'unavailable' }),
        ),
      },
      'POST /api/sync/epic': {
        body: synced(
          epicRun({
            status: 'failed',
            error_text: 'epic has ended this sign-in; connect the account again to sign back in',
            error_kind: 'credentials',
          }),
        ),
      },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    const alerts = await screen.findAllByRole('alert')
    const epic = alerts.find((alert) => alert.textContent?.startsWith('Epic Games did not sync'))
    const steam = alerts.find((alert) => alert.textContent?.startsWith('Steam did not sync'))
    expect(epic).toBeDefined()
    expect(within(epic!).getByRole('link', { name: 'Sign in again' })).toHaveAttribute(
      'href',
      '/onboarding?provider=epic',
    )
    expect(within(steam!).queryByRole('link')).not.toBeInTheDocument()
  })
})

describe('a platform the sync endpoint turned away', () => {
  it('is named, not folded into a success', async () => {
    stubFetch({
      'GET /api/sync/runs': {
        body: {
          ...IDLE,
          providers: [
            ...IDLE.providers,
            { ...IDLE.providers[0], key: 'epic', display_name: 'Epic Games' },
          ],
        },
      },
      'GET /api/accounts': { body: [account('steam', 1), account('epic', 2)] },
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: synced(run()) },
      'POST /api/sync/epic': { status: 409, body: { detail: 'account 2 is already syncing' } },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }))

    expect(await screen.findByText('Epic Games is already syncing.')).toBeInTheDocument()
    expect(screen.queryByText(/^Synced 3 games\.$/)).not.toBeInTheDocument()
  })
})

describe('connecting another platform', () => {
  it('is offered from a library that already has one', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    await screen.findByText('Portal 2')
    expect(screen.getByRole('link', { name: 'Connect an account' })).toHaveAttribute(
      'href',
      '/onboarding',
    )
  })
})

describe('hidden games', () => {
  it('can be shown again from a library where every game is hidden', async () => {
    // Hiding the only game used to leave "Nothing here yet" with no way back.
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: EMPTY },
      'GET /api/works?hidden=include': { body: THREE },
    })
    renderApp(<Library />)

    await userEvent.selectOptions(await screen.findByLabelText('Hidden games'), 'Show with the rest')

    expect(await screen.findByText('Portal 2')).toBeInTheDocument()
  })
})

describe('the filter panel', () => {
  it('asks the API for what is ticked, in its own parameter names', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'GET /api/works?kind=dlc': { body: THREE },
      'GET /api/works?kind=dlc&status=playing': { body: THREE },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByLabelText('DLC'))
    await userEvent.click(screen.getByLabelText('Playing'))

    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?kind=dlc&status=playing'),
    )
    expect(screen.getByText('Filters (2 on)')).toBeInTheDocument()
  })

  it('opens a shared link with what it got right, and drops the rest', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works?kind=dlc&metacritic_min=80': { body: THREE },
    })
    renderApp(<Library />, {
      route: '/library?kind=dlc&kind=spaceship&metacritic_min=80&year_min=1066',
    })

    expect(await screen.findByText('Portal 2')).toBeInTheDocument()
    expect(screen.getByLabelText('DLC')).toBeChecked()
    expect(screen.getByLabelText('Metacritic from')).toHaveValue(80)
    expect(calls.filter((call) => call.path.startsWith('/api/works'))).toHaveLength(1)
  })

  it('says nothing matches rather than that the library is empty', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'GET /api/works?status=mastered': { body: EMPTY },
    })
    renderApp(<Library />, { route: '/library?status=mastered' })

    expect(await screen.findByText('No games match these filters.')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing here yet/)).not.toBeInTheDocument()

    await userEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0])

    expect(await screen.findByText('Portal 2')).toBeInTheDocument()
  })

  it('does not send a range whose first number is above its second', async () => {
    const calls = stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
      'GET /api/works?year_min=2020': { body: THREE },
    })
    renderApp(<Library />)

    await userEvent.type(await screen.findByLabelText('Release year from'), '2020')
    await userEvent.type(screen.getByLabelText('Release year to'), '2010')

    expect(await screen.findByText(/is not applied/)).toBeInTheDocument()
    // The API would answer `year_min=2020&year_max=2010` with a 422.
    expect(calls.some((call) => call.path.includes('year_max'))).toBe(false)
    // Typed a digit at a time, "2" on the way to "2020" was never a request.
    expect(calls.some((call) => call.path === '/api/works?year_min=2')).toBe(false)
  })

  it('offers to clear filters the API refused', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works?platform=gog': {
        status: 422,
        body: { detail: 'not a platform: gog' },
      },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />, { route: '/library?platform=gog' })

    expect(await screen.findByRole('alert')).toHaveTextContent('not a platform')
    await userEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0])

    expect(await screen.findByText('Portal 2')).toBeInTheDocument()
  })
})

describe('the filter panel, as it is used', () => {
  const BASE = {
    'GET /api/sync/runs': { body: IDLE },
    'GET /api/accounts': { body: ACCOUNTS },
    'GET /api/works': { body: THREE },
  }

  it('offers a platform the address names even without an account on it', async () => {
    stubFetch({ ...BASE, 'GET /api/works?platform=manual': { body: THREE } })
    renderApp(<Library />, { route: '/library?platform=manual' })

    // Without a checkbox it could only be removed by clearing everything.
    expect(await screen.findByLabelText('manual')).toBeChecked()
  })

  it('asks once for a number typed a digit at a time', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?metacritic_min=8': { body: THREE },
      'GET /api/works?metacritic_min=85': { body: THREE },
    })
    renderApp(<Library />)

    await userEvent.type(await screen.findByLabelText('Metacritic from'), '85')

    await vi.waitFor(() =>
      expect(calls.some((call) => call.path === '/api/works?metacritic_min=85')).toBe(true),
    )
    expect(calls.some((call) => call.path === '/api/works?metacritic_min=8')).toBe(false)
  })

  it('shows the hours a filter applies, not a rounding of them', async () => {
    stubFetch({ ...BASE, 'GET /api/works?playtime_min=90': { body: THREE } })
    renderApp(<Library />, { route: '/library?playtime_min=90' })

    expect(await screen.findByLabelText('Hours played from')).toHaveValue(1.5)
  })

  it('does not keep a half-typed number once the field is left', async () => {
    stubFetch({ ...BASE, 'GET /api/works?kind=dlc': { body: THREE } })
    renderApp(<Library />, { route: '/library?kind=dlc' })

    await userEvent.type(await screen.findByLabelText('Release year from'), '20')
    await userEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0])

    expect(screen.getByLabelText('Release year from')).toHaveValue(null)
  })

  it('stays open when the filters it was opened for are cleared', async () => {
    stubFetch({ ...BASE, 'GET /api/works?kind=dlc': { body: THREE } })
    renderApp(<Library />)

    await userEvent.click(await screen.findByText('Filters'))
    await userEvent.click(screen.getByLabelText('DLC'))
    await userEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0])

    expect(screen.getByText('Filters').closest('details')).toHaveAttribute('open')
  })

  it('says why an inverted range is marked invalid', async () => {
    stubFetch({ ...BASE })
    renderApp(<Library />, { route: '/library?year_min=2020&year_max=2010' })

    expect(await screen.findByLabelText('Release year from')).toHaveAccessibleDescription(
      /is not applied/,
    )
  })

  it('filters on the Steam score, and says which games it leaves out', async () => {
    const calls = stubFetch({ ...BASE, 'GET /api/works?steam_min=90': { body: THREE } })
    renderApp(<Library />)

    const from = await screen.findByLabelText('Steam reviews (%) from')
    await userEvent.type(from, '90')
    await userEvent.tab()

    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?steam_min=90'),
    )
    expect(from).toHaveAccessibleDescription(/at least 10 reviews/)
  })

  it('lowers the review threshold, and says what it now counts', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?steam_min=90': { body: THREE },
      'GET /api/works?steam_min=90&steam_reviews_min=1': { body: THREE },
    })
    renderApp(<Library />, { route: '/library?steam_min=90' })

    const threshold = await screen.findByLabelText('Minimum reviews')
    expect(threshold).toHaveValue(10)
    await userEvent.clear(threshold)
    await userEvent.type(threshold, '1{Enter}')

    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain(
        '/api/works?steam_min=90&steam_reviews_min=1',
      ),
    )
    expect(screen.getByLabelText('Steam reviews (%) from')).toHaveAccessibleDescription(
      /at least 1 review\./,
    )
    // A setting, not a filter: the count is the Steam range alone.
    expect(screen.getByText('Filters (1 on)')).toBeInTheDocument()
  })

  it('turns typed hours into the minutes the API filters on', async () => {
    const calls = stubFetch({ ...BASE, 'GET /api/works?playtime_min=90': { body: THREE } })
    renderApp(<Library />)

    await userEvent.type(await screen.findByLabelText('Hours played from'), '1.5{Enter}')

    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?playtime_min=90'),
    )
  })

  it('steps back through filter changes, and through the search with them', async () => {
    stubFetch({
      ...BASE,
      'GET /api/works?kind=dlc': { body: THREE },
      'GET /api/works?q=portal': { body: THREE },
      'GET /api/works?kind=dlc&q=portal': { body: THREE },
    })
    renderApp(
      <>
        <Library />
        <Back />
      </>,
    )

    await userEvent.click(await screen.findByLabelText('DLC'))
    await userEvent.type(screen.getByLabelText('Search'), 'portal')
    await vi.waitFor(() => expect(screen.getByLabelText('Search')).toHaveValue('portal'))
    await new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS + 50))
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))

    // The entry before the tick: no filter, and no search either, because
    // the search was typed after it.
    await vi.waitFor(() => expect(screen.getByLabelText('DLC')).not.toBeChecked())
    await new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS + 50))
    expect(screen.getByLabelText('Search')).toHaveValue('')
  })
})

function Back() {
  const navigate = useNavigate()
  return <button onClick={() => void navigate(-1)}>Back</button>
}

describe('the sort control', () => {
  const BASE = {
    'GET /api/sync/runs': { body: IDLE },
    'GET /api/accounts': { body: ACCOUNTS },
    'GET /api/works': { body: THREE },
  }

  it('starts a score at the best, and turns it round on request', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?sort=metacritic&order=desc': { body: THREE },
      'GET /api/works?sort=metacritic': { body: THREE },
    })
    renderApp(<Library />)

    await userEvent.selectOptions(await screen.findByLabelText('Sort by'), 'Metacritic')
    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?sort=metacritic&order=desc'),
    )
    expect(screen.getByLabelText('Order')).toHaveDisplayValue('Highest first')

    await userEvent.selectOptions(screen.getByLabelText('Order'), 'Lowest first')
    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?sort=metacritic'),
    )
  })

  it('opens a shared link in its order, beside its filters', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?kind=dlc&sort=last_played&order=desc': { body: THREE },
    })
    renderApp(<Library />, { route: '/library?kind=dlc&sort=last_played&order=desc&sort=bogus' })

    expect(await screen.findByText('Portal 2')).toBeInTheDocument()
    expect(screen.getByLabelText('Sort by')).toHaveDisplayValue('Last played')
    expect(screen.getByLabelText('Order')).toHaveDisplayValue('Most recent first')
    expect(calls.filter((call) => call.path.startsWith('/api/works'))).toHaveLength(1)
  })

  it('is not a filter, so clearing the filters keeps it', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?kind=dlc&sort=playtime&order=desc': { body: THREE },
      'GET /api/works?sort=playtime&order=desc': { body: THREE },
    })
    renderApp(<Library />, { route: '/library?kind=dlc&sort=playtime&order=desc' })

    expect(await screen.findByText('Filters (1 on)')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))

    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?sort=playtime&order=desc'),
    )
    expect(screen.getByLabelText('Sort by')).toHaveDisplayValue('Hours played')
  })
})

describe('saved views', () => {
  const VIEWS: SavedView[] = [
    { id: 1, name: 'Backlog', position: 0, query: 'status=not_started', dropped: [] },
    {
      id: 2,
      name: 'Old one',
      position: 1,
      query: 'kind=dlc&sort=metacritic&order=desc',
      dropped: ['platform=gog'],
    },
  ]
  const BASE = {
    'GET /api/sync/runs': { body: IDLE },
    'GET /api/accounts': { body: ACCOUNTS },
    'GET /api/works': { body: THREE },
    'GET /api/views': { body: VIEWS },
  }

  it('opens as the link it stores, search and all replaced', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?q=portal': { body: THREE },
      'GET /api/works?status=not_started': { body: THREE },
    })
    renderApp(<Library />, { route: '/library?q=portal' })

    await userEvent.click(await screen.findByRole('button', { name: 'Backlog' }))

    await vi.waitFor(() =>
      expect(calls.map((call) => call.path)).toContain('/api/works?status=not_started'),
    )
    expect(screen.getByRole('button', { name: 'Backlog' })).toHaveAttribute('aria-current', 'true')
    expect(screen.getByLabelText('Not started')).toBeChecked()
    expect(screen.getByLabelText('Search')).toHaveValue('')
  })

  it('says what it opened without, while it is still what is on the screen', async () => {
    stubFetch({
      ...BASE,
      'GET /api/works?kind=dlc&sort=metacritic&order=desc': { body: THREE },
      'GET /api/works?kind=dlc&kind=game&sort=metacritic&order=desc': { body: THREE },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Old one' }))

    expect(await screen.findByText(/opened without .*platform=gog/)).toBeInTheDocument()
    expect(screen.getByLabelText('Sort by')).toHaveDisplayValue('Metacritic')

    await userEvent.click(screen.getByLabelText('Game'))
    await vi.waitFor(() => expect(screen.queryByText(/opened without/)).not.toBeInTheDocument())
  })

  it('names the view as it is now, and says nothing once it is deleted', async () => {
    const routes = {
      ...BASE,
      'GET /api/views': { body: VIEWS },
      'GET /api/works?kind=dlc&sort=metacritic&order=desc': { body: THREE },
      'PATCH /api/views/2': { body: { ...VIEWS[1], name: 'Older one' } },
      'DELETE /api/views/2': { status: 204 },
    }
    stubFetch(routes)
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Old one' }))
    await userEvent.click(screen.getByRole('button', { name: 'Edit views' }))
    routes['GET /api/views'] = { body: [VIEWS[0], { ...VIEWS[1], name: 'Older one' }] }
    const name = screen.getByLabelText('Name of “Old one”')
    await userEvent.clear(name)
    await userEvent.type(name, 'Older one{Enter}')

    expect(await screen.findByText(/“Older one” opened without/)).toBeInTheDocument()

    routes['GET /api/views'] = { body: [VIEWS[0]] }
    await userEvent.click(screen.getByRole('button', { name: 'Delete “Older one”' }))

    await vi.waitFor(() => expect(screen.queryByText(/opened without/)).not.toBeInTheDocument())
  })

  it('saves the filters and order on the screen under a name', async () => {
    const calls = stubFetch({
      ...BASE,
      'GET /api/works?kind=dlc&sort=playtime&order=desc': { body: THREE },
      'POST /api/views': { status: 201, body: { ...VIEWS[0], id: 3, name: 'DLC by play' } },
    })
    renderApp(<Library />, { route: '/library?kind=dlc&sort=playtime&order=desc&q=hades' })

    await userEvent.type(await screen.findByLabelText('Name this view'), 'DLC by play')
    await userEvent.click(screen.getByRole('button', { name: 'Save view' }))

    await vi.waitFor(() =>
      expect(calls.find((call) => call.method === 'POST')?.body).toEqual({
        name: 'DLC by play',
        query: 'kind=dlc&sort=playtime&order=desc',
      }),
    )
    await vi.waitFor(() => expect(screen.getByLabelText('Name this view')).toHaveValue(''))
  })

  it('says why a save was refused', async () => {
    stubFetch({
      ...BASE,
      'POST /api/views': { status: 409, body: { detail: 'there is already a view named “Backlog”' } },
    })
    renderApp(<Library />)

    await userEvent.type(await screen.findByLabelText('Name this view'), 'Backlog')
    await userEvent.click(screen.getByRole('button', { name: 'Save view' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already a view named')
  })

  it('stops showing a refusal once something else has worked', async () => {
    stubFetch({
      ...BASE,
      'POST /api/views': { status: 409, body: { detail: 'there is already a view named “Backlog”' } },
      'DELETE /api/views/2': { status: 204 },
    })
    renderApp(<Library />)

    await userEvent.type(await screen.findByLabelText('Name this view'), 'Backlog')
    await userEvent.click(screen.getByRole('button', { name: 'Save view' }))
    expect(await screen.findByText(/already a view named/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Edit views' }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete “Old one”' }))

    await vi.waitFor(() =>
      expect(screen.queryByText(/already a view named/)).not.toBeInTheDocument(),
    )
  })

  it('moves, renames and deletes, each through the API', async () => {
    const calls = stubFetch({
      ...BASE,
      'PUT /api/views/order': { body: [VIEWS[1], VIEWS[0]] },
      'PATCH /api/views/1': { body: { ...VIEWS[0], name: 'Pile of shame' } },
      'DELETE /api/views/2': { status: 204 },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Edit views' }))
    expect(screen.getByRole('button', { name: 'Move “Backlog” up' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Move “Backlog” down' }))
    const name = screen.getByLabelText('Name of “Backlog”')
    await userEvent.clear(name)
    await userEvent.type(name, 'Pile of shame{Enter}')
    await userEvent.click(screen.getByRole('button', { name: 'Delete “Old one”' }))

    await vi.waitFor(() =>
      expect(calls.filter((call) => call.method !== 'GET')).toEqual([
        expect.objectContaining({ method: 'PUT', body: { ids: [2, 1] } }),
        expect.objectContaining({ method: 'PATCH', body: { name: 'Pile of shame' } }),
        expect.objectContaining({ method: 'DELETE', path: '/api/views/2' }),
      ]),
    )
  })
})
