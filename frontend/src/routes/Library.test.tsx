import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import Library from '@/routes/Library'
import { renderApp, stubFetch } from '@/test/render'

const EMPTY = { works: [], next_cursor: null }

function copy(overrides: Record<string, unknown> = {}) {
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

function work(id: number, title: string, entitlements = [copy({ id, provider_title: title })]) {
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
    entitlements,
  }
}

const THREE = {
  works: [
    work(1, 'Dota 2'),
    work(2, 'Portal 2'),
    work(3, 'The Witcher 3: Wild Hunt'),
  ],
  next_cursor: null,
}

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    provider: 'steam',
    account_id: 1,
    status: 'success',
    items_seen: 3,
    items_added: 3,
    items_updated: 0,
    items_removed: 0,
    error_text: null,
    ...overrides,
  }
}

describe('library', () => {
  it('asks for a sync and reports what the run saw', async () => {
    const calls = stubFetch({
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: [run()] },
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
      'GET /api/works': { body: EMPTY },
      'POST /api/sync/steam': {
        body: [run({ status: 'failed', items_seen: 0, error_text: 'steam did not answer' })],
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
    stubFetch({ 'GET /api/works': { body: EMPTY } })
    renderApp(<Library />)

    expect(await screen.findByText(/Nothing here yet/)).toBeInTheDocument()
  })

  it('lists what came back, with the count pluralised', async () => {
    stubFetch({ 'GET /api/works': { body: THREE } })
    renderApp(<Library />)

    expect(await screen.findByText('3 games')).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: 'The Witcher 3: Wild Hunt' })).toBeInTheDocument()
  })

  it('signs out through the endpoint rather than by forgetting locally', async () => {
    const calls = stubFetch({
      'GET /api/works': { body: THREE },
      'POST /api/auth/logout': { status: 204 },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))

    // The cookie is httpOnly, so there is nothing local to forget: the session
    // ends because the server deleted the row. Where the user goes next is
    // `App.test.tsx`'s subject.
    await waitFor(() =>
      expect(calls.some((call) => call.path === '/api/auth/logout')).toBe(true),
    )
  })
})

describe('a partial run', () => {
  it('is not reported as a success', async () => {
    stubFetch({
      'GET /api/works': { body: THREE },
      'POST /api/sync/steam': { body: [run({ status: 'partial', items_seen: 2 })] },
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
    stubFetch({ 'GET /api/works': { body: THREE } })
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
    stubFetch({ 'GET /api/works': { body: THREE } })
    renderApp(<Library />)

    const link = await screen.findByRole('link', { name: 'Dota 2 on Steam' })
    expect(link).toHaveAttribute('href', 'https://store.steampowered.com/app/570')
    // The store page opens beside the library, not on top of it, and a new tab
    // must not inherit a handle on this one.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noreferrer'))
  })

  it('names a platform it cannot link rather than dropping it', async () => {
    const unlinkable = {
      works: [work(1, 'Disc Copy', [copy({ store_url: null, provider_name: 'GOG' })])],
      next_cursor: null,
    }
    stubFetch({ 'GET /api/works': { body: unlinkable } })
    renderApp(<Library />)

    // A missing store template is our gap, not the user's: they still own it
    // there, and an empty platform cell reads as if they do not.
    expect(await screen.findByText('GOG')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('shows every copy of a work owned twice', async () => {
    const both = {
      works: [
        work(1, 'Portal 2', [
          copy({ id: 10, provider_name: 'Steam' }),
          copy({ id: 11, provider: 'gog', provider_name: 'GOG', store_url: null }),
        ]),
      ],
      next_cursor: null,
    }
    stubFetch({ 'GET /api/works': { body: both } })
    renderApp(<Library />)

    // One work, two entitlements — a bundle or a rebuy. Collapsing them to the
    // first would quietly answer "where do I own this" with half the truth.
    const row = (await screen.findAllByRole('row'))[1]
    expect(within(row).getByRole('link')).toHaveTextContent('Steam')
    expect(within(row).getByText('GOG')).toBeInTheDocument()
  })
})

describe('paging', () => {
  it('follows the cursor the API handed back', async () => {
    const first = { works: [work(1, 'Dota 2')], next_cursor: 'page-2==' }
    const second = { works: [work(2, 'Portal 2')], next_cursor: null }
    const calls = stubFetch({
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
    stubFetch({ 'GET /api/works': { body: THREE } })
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
      vi.fn(async () => {
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
      // Exactly what `works.py` answers when a torn read drops every row of a
      // page: no works, and a cursor saying the library continues. It takes the
      // cursor from the last row read rather than the last row kept for this
      // case specifically, so a client that stops here truncates the library.
      'GET /api/works': { body: { works: [], next_cursor: 'page-2' } },
      'GET /api/works?cursor=page-2': { body: { works: [work(1, 'Dota 2')], next_cursor: null } },
    })
    renderApp(<Library />)

    await userEvent.click(await screen.findByRole('button', { name: 'Load more' }))

    expect(await screen.findByRole('rowheader', { name: 'Dota 2' })).toBeInTheDocument()
    // "Nothing here yet. Run a sync" over a library that is merely a page
    // further on sends the user to connect an account they already have.
    expect(screen.queryByText(/Nothing here yet/)).not.toBeInTheDocument()
  })

  it('still calls a library with nothing in it empty', async () => {
    stubFetch({ 'GET /api/works': { body: EMPTY } })
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
        if (path.startsWith('/api/sync')) {
          syncing = true
          return new Response(JSON.stringify([run()]), { status: 200 })
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
          }),
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
