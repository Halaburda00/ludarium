import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  Account,
  EntitlementSummary,
  SyncOverview,
  SyncResult,
  SyncRun,
  WorkSummary,
  WorksPage,
} from '@/lib/queries'
import { ENRICHMENT_POLL_MS } from '@/lib/queries'
import Library from '@/routes/Library'
import { renderApp, stubFetch } from '@/test/render'

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
    expect(screen.getByRole('rowheader', { name: 'The Witcher 3: Wild Hunt' })).toBeInTheDocument()
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
})

describe('the table', () => {
  it('gives every game a row with its title and its platform', async () => {
    stubFetch({
      'GET /api/sync/runs': { body: IDLE },
      'GET /api/accounts': { body: ACCOUNTS },
      'GET /api/works': { body: THREE },
    })
    renderApp(<Library />)

    // Found by role rather than by text: a `<div>` full of titles would satisfy
    // `getByText` just as well, and the issue asks for a table.
    const rows = await screen.findAllByRole('row')
    expect(within(rows[0]).getByRole('columnheader', { name: 'Title' })).toBeInTheDocument()
    expect(within(rows[0]).getByRole('columnheader', { name: 'Platform' })).toBeInTheDocument()
    expect(rows).toHaveLength(4)
    expect(within(rows[1]).getByRole('rowheader')).toHaveTextContent('Dota 2')
    expect(within(rows[1]).getByRole('link')).toHaveTextContent('Steam')
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
    expect(within(screen.getByRole('table')).queryByRole('link')).not.toBeInTheDocument()
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
    const row = (await screen.findAllByRole('row'))[1]
    expect(within(row).getByRole('link')).toHaveTextContent('Steam')
    expect(within(row).getByText('GOG')).toBeInTheDocument()
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
    const dota = screen.getAllByRole('row')[2]
    expect(within(dota).getByText('No score')).toBeInTheDocument()
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

    // Honest about what it has: one page of an unknown number is not "1 game".
    expect(await screen.findByText('1 game so far')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))

    expect(await screen.findByText('2 games')).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: 'Portal 2' })).toBeInTheDocument()
    // Appended, not replaced: the first page is still on screen.
    expect(screen.getByRole('rowheader', { name: 'Dota 2' })).toBeInTheDocument()
    expect(calls.filter((call) => call.path.startsWith('/api/works'))).toHaveLength(2)
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
    expect(await screen.findByRole('rowheader', { name: 'Dota 2' })).toBeInTheDocument()
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

    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }))

    expect(await screen.findByRole('rowheader', { name: 'Dota 2' })).toBeInTheDocument()
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

    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }))
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
